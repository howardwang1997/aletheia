"""Phase 2 increment 3: GitHub App IAM — policy gate (naming / daily cap /
never-delete), the dry-run backend's recorded ops, the real App-auth client
wiring (mocked githubkit, no network), and the driver creating a repo + branch +
PR-per-experiment over a full dry-run loop.
"""

from __future__ import annotations

import sys
import types

import pytest

from aletheia.data.registry import register_dataset
from aletheia.db import create_all, session_scope
from aletheia.iam import policy
from aletheia.iam.github_app import (
    DryRunGitHubBackend,
    RealGitHubBackend,
    get_github_backend,
)
from aletheia.memory.ledger import Experiment
from aletheia.memory.service import create_run, finalize_plan
from aletheia.scheduler.driver import ExperimentDriver


# --- policy ---------------------------------------------------------------
def test_repo_and_branch_naming():
    assert policy.repo_name("materials", "Band Gap Prediction!").startswith("aletheia-materials-")
    b = policy.branch_name("abc123def456ghi", "tune the GBM")
    assert b.startswith("exp/abc123def456-") and " " not in b


def test_check_repo_create_namespace_and_cap():
    assert policy.check_repo_create("aletheia-materials-x", 0).allow
    # outside the prefix namespace -> denied
    assert not policy.check_repo_create("some-other-repo", 0).allow
    # a bare-prefix repo is allowed; a prefix-glued name is not
    assert policy.check_repo_create("aletheia", 0).allow
    assert not policy.check_repo_create("aletheiafoo", 0).allow
    # daily cap (default 5) honored
    assert not policy.check_repo_create("aletheia-materials-x", 5).allow


def test_push_and_delete_guards():
    assert policy.check_push("aletheia-materials-x").allow
    assert not policy.check_push("victim-repo").allow
    assert not policy.check_delete("aletheia-materials-x").allow  # never autonomous


# --- backend resolution + dry-run recording -------------------------------
def test_get_backend_dry_run_and_unconfigured(monkeypatch):
    import aletheia.iam.github_app as mod

    assert isinstance(get_github_backend(dry_run=True), DryRunGitHubBackend)
    monkeypatch.setattr(
        mod, "get_settings",
        lambda: types.SimpleNamespace(github_configured=False, github_owner=None),
    )
    assert isinstance(get_github_backend(dry_run=False), DryRunGitHubBackend)


def test_dry_run_backend_records_ops():
    gh = DryRunGitHubBackend(owner="acme")
    repo = gh.ensure_repo("aletheia-mat-x", private=True)
    assert repo.full_name == "acme/aletheia-mat-x" and repo.created
    gh.ensure_branch(repo.full_name, "exp/1-x")
    gh.put_file(repo.full_name, "report.md", "# hi", message="m", branch="exp/1-x")
    pr = gh.open_pr(repo.full_name, head="exp/1-x", base="main", title="t", body="b")
    assert pr.html_url.endswith("/pull/1")
    ops = [c["op"] for c in gh.calls]
    assert ops == ["ensure_repo", "ensure_branch", "put_file", "open_pr"]


# --- real backend: App-auth client wiring (mocked, no network) -------------
def test_real_backend_builds_app_auth_client_and_creates_repo(monkeypatch):
    import aletheia.iam.github_app as mod

    seen = {}

    class _Resp:
        def __init__(self, data):
            self.parsed_data = data

    class _Repos:
        def get(self, owner, name):
            raise RuntimeError("404 not found")  # -> triggers create

        def create_for_authenticated_user(self, **kwargs):
            seen["create"] = kwargs
            return _Resp(types.SimpleNamespace(html_url="https://github.com/me/aletheia-mat-x"))

    class _GH:
        def __init__(self, auth):
            seen["auth"] = auth
            self.rest = types.SimpleNamespace(repos=_Repos())

    def _auth_strategy(app_id, pem, installation_id):
        seen["auth_args"] = (app_id, pem, installation_id)
        return "app-auth"

    fake = types.ModuleType("githubkit")
    fake.GitHub = _GH
    fake.AppInstallationAuthStrategy = _auth_strategy
    monkeypatch.setitem(sys.modules, "githubkit", fake)

    monkeypatch.setattr(
        mod, "get_settings",
        lambda: types.SimpleNamespace(
            github_app_id="123",
            github_app_installation_id="999",
            github_private_key_pem=lambda: "PEM",
            github_owner="me",
            github_owner_is_org=False,
        ),
    )

    backend = RealGitHubBackend()
    repo = backend.ensure_repo("aletheia-mat-x", description="d", private=True)
    assert repo.html_url == "https://github.com/me/aletheia-mat-x" and repo.created
    assert seen["auth_args"] == ("123", "PEM", 999)  # installation id coerced to int
    assert seen["create"]["name"] == "aletheia-mat-x" and seen["create"]["private"] is True


# --- driver wires repo + branch + PR over a full dry-run loop --------------
@pytest.mark.asyncio
async def test_driver_creates_repo_branch_pr_dry_run(monkeypatch):
    # isolate from the daily repo-creation cap (the persistent dev DB accumulates
    # iam_repo_created events across runs; the cap working is covered in the policy test)
    monkeypatch.setattr(policy, "created_repos_last_24h", lambda: 0)
    create_all()
    run_id = create_run("iam e2e", domain="materials", status="scoping")
    register_dataset(run_id, "benchmark", ref="matbench_expt_gap", status="ready")
    exp_id = finalize_plan(
        run_id,
        {
            "objective": "predict band gap from composition",
            "domain": "materials",
            "direction": "composition ML",
            "dataset": "matbench_expt_gap",
        },
    )

    driver = ExperimentDriver(run_id, dry_run=True)
    await driver.run()

    # the dry-run backend recorded the full repo/branch/file/PR lifecycle
    ops = [c["op"] for c in driver.gh.calls]
    assert ops == ["ensure_repo", "ensure_branch", "put_file", "open_pr"]

    # repo + branch persisted on the experiment (audit trail)
    with session_scope() as s:
        exp = s.get(Experiment, exp_id)
        assert exp.code_repo and exp.code_repo.startswith("aletheia-dry/aletheia-materials")
        assert exp.code_branch and exp.code_branch.startswith("exp/")
