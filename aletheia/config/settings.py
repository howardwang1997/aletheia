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


class CriticsConfig(BaseModel):
    panel: list[CriticConfig] = Field(default_factory=list)
    consensus: ConsensusConfig = Field(default_factory=ConsensusConfig)

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

    # --- Claude runtime ---
    claude_auth_mode: Literal["subscription", "api_key"] = "subscription"
    claude_model: str = "claude-opus-4-7"

    # --- budget guardrails (per run) ---
    budget_usd: float = 20.0
    budget_gpu_hours: float = 4.0
    max_concurrent_jobs: int = 2
    wall_clock_hours: float = 24.0

    # --- vendor keys (read without the ALETHEIA_ prefix) ---
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    claude_code_oauth_token: str | None = Field(default=None, alias="CLAUDE_CODE_OAUTH_TOKEN")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    google_api_key: str | None = Field(default=None, alias="GOOGLE_API_KEY")
    deepseek_api_key: str | None = Field(default=None, alias="DEEPSEEK_API_KEY")
    zhipu_api_key: str | None = Field(default=None, alias="ZHIPU_API_KEY")
    feishu_webhook_url: str | None = Field(default=None, alias="FEISHU_WEBHOOK_URL")
    mp_api_key: str | None = Field(default=None, alias="MP_API_KEY")

    @property
    def critics(self) -> CriticsConfig:
        return CriticsConfig.load()

    def vendor_key(self, vendor_id: str) -> str | None:
        return {
            "openai": self.openai_api_key,
            "gemini": self.google_api_key,
            "deepseek": self.deepseek_api_key,
            "zhipu": self.zhipu_api_key,
        }.get(vendor_id)


@functools.lru_cache
def get_settings() -> Settings:
    return Settings()
