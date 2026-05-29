"""Central configuration for Aletheia.

All runtime config lives here (loaded from environment / `.env`), plus the
critic-panel roster loaded from `config/critics.yaml`. Keeping this in one place
makes the auth/model/budget switches explicit and testable.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = PACKAGE_DIR.parent
CRITICS_YAML = PACKAGE_DIR / "config" / "critics.yaml"


class CriticConfig(BaseModel):
    """One vendor entry in the critic panel."""

    id: str
    enabled: bool = True
    transport: Literal["api", "cli"] = "api"
    model: str
    base_url: str | None = None


class ConsensusConfig(BaseModel):
    rule: Literal["any_blocker", "majority"] = "any_blocker"
    max_design_iterations: int = 3


class RoundsConfig(BaseModel):
    """Dynamic peer-review rebuttal depth.

    ``importance`` maps a gate target (design/results/direction) to the MAX number of
    rounds; ``default`` covers anything unlisted. When ``dynamic`` is on, rounds run
    only while reviewer disagreement exceeds ``disagreement_threshold`` (fraction of
    reviewers dissenting from the majority verdict), early-stopping on convergence.
    """

    dynamic: bool = True
    disagreement_threshold: float = 0.34
    importance: dict[str, int] = Field(
        default_factory=lambda: {"design": 5, "results": 5, "direction": 1, "default": 1}
    )

    def max_rounds(self, target: str) -> int:
        return int(self.importance.get(target, self.importance.get("default", 1)))


class CriticsConfig(BaseModel):
    panel: list[CriticConfig] = Field(default_factory=list)
    consensus: ConsensusConfig = Field(default_factory=ConsensusConfig)
    rounds: RoundsConfig = Field(default_factory=RoundsConfig)

    @classmethod
    def load(cls, path: Path = CRITICS_YAML) -> "CriticsConfig":
        if not path.exists():
            return cls()
        data = yaml.safe_load(path.read_text()) or {}
        return cls.model_validate(data)

    @property
    def active(self) -> list[CriticConfig]:
        return [c for c in self.panel if c.enabled]


class Settings(BaseSettings):
    """Environment-driven settings. Prefix `ALETHEIA_`; vendor keys use their
    conventional names (e.g. `ANTHROPIC_API_KEY`)."""

    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_prefix="ALETHEIA_",
        extra="ignore",
        case_sensitive=False,
    )

    # --- database ---
    database_url: str = "postgresql+psycopg://aletheia:aletheia@localhost:5432/aletheia"

    # --- semantic recall (pgvector) ---
    # backend: "local" = sentence-transformers (offline, production default);
    #          "hash"  = deterministic offline stub (tests / dry-run, zero spend).
    # embedding_dim MUST match the backend's output dim and is fixed at table
    # creation — keep MiniLM's 384 unless you also recreate the embeddings table.
    embedding_backend: Literal["local", "hash"] = "local"
    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_dim: int = 384
    recall_k: int = 5  # default neighbours returned by memory.recall

    # --- Claude runtime ---
    claude_auth_mode: Literal["subscription", "api_key"] = "subscription"
    claude_model: str = "claude-opus-4-7"

    # --- Codex CLI (critic transport "cli": GPT-5.5 on the OpenAI Coding Plan) ---
    codex_command: str = "codex"
    codex_timeout_s: float = 240.0

    # --- budget guardrails (per run) ---
    budget_usd: float = 20.0
    budget_gpu_hours: float = 4.0
    max_concurrent_jobs: int = 2
    wall_clock_hours: float = 24.0
    est_stage_cost_usd: float = 0.10  # estimated cost charged per Opus reasoning stage

    # --- vendor keys (read without the ALETHEIA_ prefix) ---
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    claude_code_oauth_token: str | None = Field(default=None, alias="CLAUDE_CODE_OAUTH_TOKEN")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    google_api_key: str | None = Field(default=None, alias="GOOGLE_API_KEY")
    deepseek_api_key: str | None = Field(default=None, alias="DEEPSEEK_API_KEY")
    zhipu_api_key: str | None = Field(default=None, alias="ZHIPU_API_KEY")
    feishu_webhook_url: str | None = Field(default=None, alias="FEISHU_WEBHOOK_URL")
    mp_api_key: str | None = Field(default=None, alias="MP_API_KEY")

    # --- per-vendor endpoint overrides (OpenAI-compatible critics) ---
    zhipu_base_url: str | None = Field(default=None, alias="ZHIPU_BASE_URL")
    deepseek_base_url: str | None = Field(default=None, alias="DEEPSEEK_BASE_URL")

    # --- GitHub App (IAM: repo-per-project, branch + PR per experiment) ---
    # A GitHub App (not a PAT) gives the autonomous agent short-lived, fine-grained,
    # per-repo access with no human seat and central revocation. Repos are created
    # under the account/org where the App is installed.
    github_app_id: str | None = Field(default=None, alias="GITHUB_APP_ID")
    github_app_installation_id: str | None = Field(default=None, alias="GITHUB_APP_INSTALLATION_ID")
    github_app_private_key: str | None = Field(default=None, alias="GITHUB_APP_PRIVATE_KEY")
    github_app_private_key_path: str | None = Field(default=None, alias="GITHUB_APP_PRIVATE_KEY_PATH")
    github_owner: str | None = Field(default=None, alias="GITHUB_OWNER")  # else derived from install
    github_owner_is_org: bool = Field(default=False, alias="GITHUB_OWNER_IS_ORG")  # org vs personal acct

    # --- IAM policy (irreversible-op guardrails) ---
    iam_repo_prefix: str = "aletheia"  # repos the agent may touch must start with this
    iam_repo_visibility: Literal["private", "public"] = "private"
    iam_create_repo_daily_cap: int = 5  # rate-limit repo creation; never auto-delete

    @property
    def critics(self) -> CriticsConfig:
        return CriticsConfig.load()

    @property
    def github_configured(self) -> bool:
        return bool(
            self.github_app_id
            and self.github_app_installation_id
            and (self.github_app_private_key or self.github_app_private_key_path)
        )

    def github_private_key_pem(self) -> str | None:
        """The App private key PEM — inline value wins, else read from the path."""
        if self.github_app_private_key:
            return self.github_app_private_key
        if self.github_app_private_key_path:
            from pathlib import Path

            p = Path(self.github_app_private_key_path).expanduser()
            return p.read_text() if p.exists() else None
        return None

    def vendor_key(self, vendor_id: str) -> str | None:
        return {
            "openai": self.openai_api_key,
            "gemini": self.google_api_key,
            "deepseek": self.deepseek_api_key,
            "zhipu": self.zhipu_api_key,
        }.get(vendor_id)

    def vendor_base_url(self, vendor_id: str) -> str | None:
        """Env override for an OpenAI-compatible vendor's endpoint (else None ->
        the provider falls back to the base_url in critics.yaml)."""
        return {
            "zhipu": self.zhipu_base_url,
            "deepseek": self.deepseek_base_url,
        }.get(vendor_id)


@functools.lru_cache
def get_settings() -> Settings:
    return Settings()
