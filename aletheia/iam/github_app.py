"""GitHub App backend for IAM — repo-per-project, branch + PR per experiment.

A GitHub App (not a PAT) mints short-lived installation tokens with fine-grained,
per-repo scope, no human seat, and central revocation — the right credential model
for a 24/7 autonomous actor that creates repos and pushes code.

``DryRunGitHubBackend`` records the intended operations and returns synthetic URLs
so the whole lifecycle is verifiable with zero external calls; ``RealGitHubBackend``
talks to GitHub via githubkit (lazy-imported). ``get_github_backend`` resolves the
right one and degrades to dry-run whenever the App isn't configured.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from aletheia.config import get_settings


@dataclass
class RepoResult:
    owner: str
    name: str
    html_url: str
    created: bool  # True if we created it this call, False if it already existed

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass
class PRResult:
    number: int
    html_url: str
    created: bool


class GitHubBackend(ABC):
    name: str = "base"

    @abstractmethod
    def ensure_repo(self, name: str, *, description: str = "", private: bool = True) -> RepoResult: ...

    @abstractmethod
    def ensure_branch(self, repo_full_name: str, branch: str, *, base: str = "main") -> str: ...

    @abstractmethod
    def put_file(
        self, repo_full_name: str, path: str, content: str, *, message: str, branch: str
    ) -> None: ...

    @abstractmethod
    def open_pr(
        self, repo_full_name: str, *, head: str, base: str, title: str, body: str
    ) -> PRResult: ...


class DryRunGitHubBackend(GitHubBackend):
    """Records intended ops; performs none. Used in dry-run and when the App is
    unconfigured so the loop still produces a code_repo/branch/PR audit trail."""

    name = "dry_run"

    def __init__(self, owner: str = "aletheia-dry") -> None:
        self.owner = owner
        self.calls: list[dict[str, Any]] = []

    def ensure_repo(self, name: str, *, description: str = "", private: bool = True) -> RepoResult:
        self.calls.append({"op": "ensure_repo", "name": name, "private": private})
        return RepoResult(self.owner, name, f"https://github.com/{self.owner}/{name}", created=True)

    def ensure_branch(self, repo_full_name: str, branch: str, *, base: str = "main") -> str:
        self.calls.append({"op": "ensure_branch", "repo": repo_full_name, "branch": branch})
        return branch

    def put_file(
        self, repo_full_name: str, path: str, content: str, *, message: str, branch: str
    ) -> None:
        self.calls.append({"op": "put_file", "repo": repo_full_name, "path": path, "branch": branch})

    def open_pr(
        self, repo_full_name: str, *, head: str, base: str, title: str, body: str
    ) -> PRResult:
        self.calls.append({"op": "open_pr", "repo": repo_full_name, "head": head, "title": title})
        return PRResult(number=1, html_url=f"https://github.com/{repo_full_name}/pull/1", created=True)


class RealGitHubBackend(GitHubBackend):
    """Talks to GitHub via githubkit using App installation auth. Lazy-imports
    githubkit so the dependency is only required when actually used."""

    name = "github_app"

    def __init__(self) -> None:
        s = get_settings()
        pem = s.github_private_key_pem()
        if not (s.github_app_id and s.github_app_installation_id and pem):
            raise RuntimeError("GitHub App is not fully configured")
        self.owner = s.github_owner
        self.owner_is_org = s.github_owner_is_org
        self._gh = None
        self._app_id = s.github_app_id
        self._installation_id = s.github_app_installation_id
        self._pem = pem

    def _client(self):
        if self._gh is None:
            from githubkit import AppInstallationAuthStrategy, GitHub

            self._gh = GitHub(
                AppInstallationAuthStrategy(
                    self._app_id, self._pem, int(self._installation_id)
                )
            )
            if not self.owner:
                # derive the owner (account the App is installed on)
                inst = self._gh.rest.apps.get_installation(int(self._installation_id))
                self.owner = inst.parsed_data.account.login
        return self._gh

    def ensure_repo(self, name: str, *, description: str = "", private: bool = True) -> RepoResult:
        gh = self._client()
        try:
            existing = gh.rest.repos.get(self.owner, name)
            d = existing.parsed_data
            return RepoResult(self.owner, name, d.html_url, created=False)
        except Exception:
            pass  # not found -> create
        if self.owner_is_org:
            resp = gh.rest.repos.create_in_org(
                self.owner, name=name, description=description, private=private, auto_init=True
            )
        else:
            resp = gh.rest.repos.create_for_authenticated_user(
                name=name, description=description, private=private, auto_init=True
            )
        d = resp.parsed_data
        return RepoResult(self.owner, name, d.html_url, created=True)

    def ensure_branch(self, repo_full_name: str, branch: str, *, base: str = "main") -> str:
        gh = self._client()
        owner, name = repo_full_name.split("/", 1)
        try:
            gh.rest.repos.get_branch(owner, name, branch)
            return branch  # already exists
        except Exception:
            pass
        base_ref = gh.rest.repos.get_branch(owner, name, base)
        sha = base_ref.parsed_data.commit.sha
        gh.rest.git.create_ref(owner, name, ref=f"refs/heads/{branch}", sha=sha)
        return branch

    def put_file(
        self, repo_full_name: str, path: str, content: str, *, message: str, branch: str
    ) -> None:
        import base64

        gh = self._client()
        owner, name = repo_full_name.split("/", 1)
        b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
        kwargs: dict[str, Any] = {"message": message, "content": b64, "branch": branch}
        try:  # update needs the existing blob sha
            existing = gh.rest.repos.get_content(owner, name, path, ref=branch)
            kwargs["sha"] = existing.parsed_data.sha
        except Exception:
            pass
        gh.rest.repos.create_or_update_file_contents(owner, name, path, **kwargs)

    def open_pr(
        self, repo_full_name: str, *, head: str, base: str, title: str, body: str
    ) -> PRResult:
        gh = self._client()
        owner, name = repo_full_name.split("/", 1)
        resp = gh.rest.pulls.create(owner, name, title=title, head=head, base=base, body=body)
        d = resp.parsed_data
        return PRResult(number=d.number, html_url=d.html_url, created=True)


def get_github_backend(*, dry_run: bool = False) -> GitHubBackend:
    """Real App backend when configured and not dry-run; else a recording dry-run
    backend so the lifecycle still emits a repo/branch/PR audit trail with no
    external calls."""
    s = get_settings()
    if dry_run or not s.github_configured:
        return DryRunGitHubBackend(owner=s.github_owner or "aletheia-dry")
    return RealGitHubBackend()
