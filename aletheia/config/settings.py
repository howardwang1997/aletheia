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

    # --- reproducibility identity ---
    # Formal scientific runs freeze an immutable manifest before the first scientific action.
    # Development/dry runs may use the same mechanism, but only a clean git tree qualifies for a
    # release-grade frozen manifest unless this explicit local escape hatch is enabled.
    allow_dirty_frozen_manifest: bool = False

    # --- semantic recall (pgvector) ---
    # backend: "local" = sentence-transformers (offline, production default);
    #          "hash"  = deterministic offline stub (tests / dry-run, zero spend).
    # embedding_dim MUST match the backend's output dim and is fixed at table
    # creation — keep MiniLM's 384 unless you also recreate the embeddings table.
    embedding_backend: Literal["local", "hash"] = "local"
    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_dim: int = 384
    recall_k: int = 5  # default neighbours returned by memory.recall

    # --- orchestrator runtime (the scientist/author, independent from critic vendors) ---
    orchestrator_provider: Literal["claude", "openai"] = "claude"

    # Claude Agent SDK path (subscription login or API key).
    claude_auth_mode: Literal["subscription", "api_key"] = "subscription"
    claude_model: str = "claude-opus-4-8"

    # OpenAI supports the same two auth choices as Claude: a ChatGPT subscription through the
    # official Codex CLI, or a metered API key through the Responses API. GPT-5.6 Sol is the
    # frontier-capability default; keep the model configurable so evals can pin another model.
    openai_auth_mode: Literal["subscription", "api_key"] = "subscription"
    openai_model: str = "gpt-5.6-sol"
    openai_reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] = "high"
    openai_max_output_tokens: int = 16_384

    # --- Codex CLI (OpenAI subscription orchestrator + critic transport "cli") ---
    codex_command: str = "codex"
    codex_timeout_s: float = 240.0

    # --- Claude CLI (critic transport "cli": Claude on the machine's Coding Plan login,
    # the same subscription the orchestrator uses — the most reliable critic credential) ---
    claude_command: str = "claude"
    claude_cli_timeout_s: float = 240.0

    # min seconds between calls to one OpenAI-compatible critic vendor (serialized).
    # GLM's Coding Plan is slow + rate-limits bursts, so we space its calls out.
    critic_vendor_min_interval_s: float = 3.0

    # rounds of adversarial debate (skeptic attacks novelty/rigor -> proposer revises) the
    # IDEATION stage runs to scrutinize + strengthen a hypothesis before it is committed.
    ideation_debate_rounds: int = 2

    # min DISTINCT critic vendors that must actually review for a gate to count as a real
    # cross-vendor review; below this, the gate is `degraded_review` and a gate-derived
    # claim is capped at `weak` (single-vendor / same-vendor self-review is not strong evidence).
    min_review_vendors: int = 2

    # min citable papers a survey must retrieve for the prior-work grounding to be "healthy";
    # below this (but >0) the run proceeds with a recorded weak-grounding limitation so a
    # near-empty literature search is treated as WEAK evidence, never as confirmed novelty.
    min_survey_papers: int = 3

    # --- literature reranking: reorder the multi-source candidate pool by genuine query
    # relevance with a CPU cross-encoder + drop off-topic hits. ms-marco-MiniLM is the
    # fast CPU default; swap to BAAI/bge-reranker-v2-m3 or gte-reranker-modernbert-base
    # for higher quality. Best-effort: if the model can't load, the merge order is kept.
    reranker_enabled: bool = True
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    reranker_min_relevance: float = 0.05  # sigmoid(score) below this is off-topic -> dropped

    # --- coder sandbox (executing AI-authored model code) ---
    # The AST policy is defense in depth. Every AI-authored execution path defaults
    # to the hard Docker boundary below; host execution is development-only.
    sandbox_preflight_timeout_s: float = 120.0  # bounded allowance for first image cold-start
    sandbox_timeout_s: float = 600.0  # wall-clock kill for a training subprocess
    sandbox_cpu_seconds: int = 600  # RLIMIT_CPU
    sandbox_max_memory_mb: int = 4096  # RLIMIT_AS (best-effort on macOS)
    sandbox_output_limit_bytes: int = 262_144
    coder_enabled: bool = True  # author a solution.py each run (falls back if it fails the gate)

    # --- AI-authored demonstration (the frontier path) ---
    # The AI writes the discriminating computation itself (compute_demonstration); the harness
    # applies its pre-registered decision rule + negative control. Falls back to a registered
    # capability when disabled or when authoring/gating fails (fail closed).
    ai_demonstration_enabled: bool = True
    demonstration_min_samples: int = 20  # min n on BOTH the test and control sides (probe)
    demonstration_timeout_s: float = 120.0  # wall-clock for the AI demonstration subprocess
    demonstration_audit_enabled: bool = True  # independent cross-vendor audit of the AI demo
    # Default routing is REGISTERED-FIRST: a hand-built, already-audited capability grounds the
    # claim when one keyword-matches, and the AI-authored path is reserved for claims nothing can
    # currently ground. Set True (or tag a demonstration spec with ``authoring="ai"``) to PREFER
    # the AI-authored path even when a registered capability matches — the frontier override.
    # Authoring failure still falls back to the registered capability (fail closed).
    demonstration_prefer_authored: bool = False
    # K1 explore->confirm seal: the AI authors an EXPLORATION probe on a disjoint explore subset,
    # calibrates its pre-registered threshold to what it observed, then the harness CONFIRMS on a
    # held-out confirm subset the authoring never saw. On any failure (infeasible split, exploration
    # worker/sandbox error) the run degrades to the blind authoring path, but the formulation claim
    # is then capped below `strong` (no seal = no independent-of-noise verification).
    demonstration_explore_confirm_enabled: bool = True

    # --- compute backend ---
    # "local"  = restricted subprocess on the host (development only; never unattended real science).
    # "docker" = HARD sandbox: host featurizes + stages X/y, a light no-network
    #            container runs only the (untrusted) model code via train_evaluate.
    compute_backend: Literal["local", "docker"] = "docker"
    # Every direct authored-code path (smoke, exploration, demonstration) uses this separate,
    # unified boundary. ``local_dev`` is an explicit test/developer escape hatch only.
    authored_code_backend: Literal["docker", "local_dev"] = "docker"
    allow_unsafe_host_authored_code: bool = False
    sandbox_docker_command: str = "docker"
    sandbox_docker_image: str = "aletheia-sandbox:latest"
    # F7 research-plane image. Unlike ``sandbox_docker_image`` this must not bake in the
    # Aletheia package, evaluator runner, scorer code, or hidden assets.
    evaluator_agent_docker_image: str = "aletheia-evaluator-agent:latest"
    # Reviewed scientific runtime for the default ScienceAgentBench CC-BY mini-suite.
    # The adapter resolves this tag to an immutable image ID before freezing a suite.
    scienceagentbench_docker_image: str = "aletheia-scienceagentbench:latest"
    # Reviewed offline runtime for the Asta CORE-Bench-Hard MIT/CC0 validation mini-suite.
    corebench_docker_image: str = "aletheia-corebench:latest"
    # DiscoveryWorld uses two distinct images: a neutral policy runtime with no evaluator/source
    # code and a trusted official hidden-world runtime. Both are resolved to immutable IDs.
    discoveryworld_candidate_docker_image: str = "aletheia-discoveryworld-candidate:latest"
    discoveryworld_docker_image: str = "aletheia-discoveryworld:latest"
    sandbox_docker_cpus: float = 2.0
    sandbox_docker_pids: int = 256
    sandbox_allow_network: bool = False  # deprecated compatibility field; authored code is always offline

    # --- budget guardrails (per run) ---
    budget_usd: float = 20.0
    budget_gpu_hours: float = 4.0
    max_concurrent_jobs: int = 2
    wall_clock_hours: float = 24.0
    est_stage_cost_usd: float = 0.10  # fallback estimate when the provider reports no cost
    # optional hard cap on a run's TOTAL tokens (in+out+cache). None = off. Tokens — not USD —
    # are what the rolling subscription usage window meters (cost reads ~0 under subscription
    # auth), so this is the knob that bounds a run's contribution to the 5-hour limit.
    token_cap_per_run: int | None = None
    # checkpoint / resume: persist each successful worker result keyed by provider + content hash so a
    # re-run of the SAME run_id can replay for free. `enabled` controls WRITING the cache (harmless
    # always-on — a fresh run_id has an empty cache); `read` is set True only by a resume launch, so a
    # normal run never short-circuits on the cache (no within-run collisions).
    resume_cache_enabled: bool = True
    resume_cache_read: bool = False
    # weak-network resilience: cap how many live provider calls run CONCURRENTLY, so a fragile
    # proxy/tunnel isn't asked to hold many long-lived streams at once (the main ECONNRESET trigger).
    # None = unlimited (default). And retry a failed call this many OUTER attempts (each opens a fresh
    # connection and wraps the SDK's own internal retries), with linear backoff between attempts.
    max_concurrent_workers: int | None = None
    worker_max_attempts: int = 2
    worker_backoff_s: float = 8.0
    # per-call override for the discriminating-demonstration authoring — the longest, most critical
    # stream and the one that degrades on a proxy reset. Give just that call more patient attempts to
    # land one clean stream, while everything else keeps the frugal `worker_max_attempts`. None = use
    # `worker_max_attempts` for it too.
    authoring_max_attempts: int | None = None
    # how many CONTENT rounds the demonstration authoring gets within one campaign round: each retry
    # re-authors with the prior rejection reason (control-not-silent / threshold-doomed / runtime
    # error) fed back, so more rounds = more chances to fix a flagged DESIGN flaw before the round is
    # written off as undemonstrated. Distinct from `authoring_max_attempts` (network retries per call).
    demonstration_authoring_rounds: int = 2
    # AUTONOMOUS DISCOVERY stage (off by default): when enabled, the driver runs the divergent
    # ideate -> self-triage loop (aletheia.research.discovery) IN PLACE OF single-shot ideation —
    # it generates bold candidates with code, self-screens them (code/run/hold/non-trivial/grounded/
    # novel), and adopts a survivor's hypothesis + its already-verified demonstration code. A
    # discovery-sourced hypothesis skips the direction gate (the loop already cleared the novelty gate).
    discovery_enabled: bool = False
    discovery_k_survivors: int = 1
    discovery_max_rounds: int = 4
    discovery_ideator_vendor: str = "grok"  # the (non-author) vendor that proposes candidates+code
    discovery_coauthor: bool = False  # grok proposes the ANGLE; the orchestrator writes the code.
    # novelty review excludes BOTH author vendors.
    # window-aware graceful stop: when the SDK's LIVE 5-hour-window reading reaches this fill (or is
    # 'rejected'), pause+checkpoint BEFORE launching another expensive stage, instead of slamming the
    # window to the wall and dying mid-stream. Resume on a fresh window replays the prefix for 0 tokens,
    # so work spreads across windows rather than burning one. None = off (0..1, e.g. 0.85).
    window_stop_utilization: float | None = None
    max_experiments_per_campaign: int = 3  # one Run -> up to N linked experiments (go/no-go decides)
    min_experiments_per_campaign: int = 1  # validation runs may require a genuinely multi-round trace
    # Epistemic Seal v2: roles are allocated ONCE before ideation. Confirmation batches are
    # mutually exclusive across adaptive rounds; the final holdout is opened exactly once.
    campaign_split_seed: int = 20260812
    campaign_seal_v2_enabled: bool = True
    campaign_explore_fraction: float = 0.50
    campaign_final_holdout_fraction: float = 0.20
    campaign_family_alpha: float = 0.05
    campaign_final_alpha: float = 0.01
    campaign_external_validation_required: bool = False
    campaign_external_alpha: float = 0.05
    campaign_external_timeout_s: float = 600.0
    # K2 S3.5: a paradigm round that produces NO demonstration (e.g. the AI could not author a
    # threshold consistent with its own exploration) is an informative negative — the campaign may
    # PIVOT to a different hypothesis this many times before failing closed (pausing the run).
    campaign_max_pivots: int = 2

    # --- hypothesis scorecard (gate low-value experiments before spending compute) ---
    # scores are 0..1; a hypothesis is blocked if it scores below these on the two
    # dimensions that make an experiment worth running at all.
    hypothesis_min_novelty: float = 0.4
    hypothesis_min_eval_clarity: float = 0.4
    campaign_min_eig: float = 0.3  # stop the campaign when expected information gain drops below this

    # --- reproduction pass (a metric claim earns `strong` only when re-run confirms it) ---
    reproduction_enabled: bool = True
    reproduction_rel_tol: float = 0.05  # relative tolerance on the headline metric

    # --- vendor keys (read without the ALETHEIA_ prefix) ---
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    claude_code_oauth_token: str | None = Field(default=None, alias="CLAUDE_CODE_OAUTH_TOKEN")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_base_url: str | None = Field(default=None, alias="OPENAI_BASE_URL")
    google_api_key: str | None = Field(default=None, alias="GOOGLE_API_KEY")
    deepseek_api_key: str | None = Field(default=None, alias="DEEPSEEK_API_KEY")
    zhipu_api_key: str | None = Field(default=None, alias="ZHIPU_API_KEY")
    grok_api_key: str | None = Field(default=None, alias="GROK_API_KEY")
    # optional: a free Semantic Scholar key removes the unauthenticated 429s (keyless works too)
    semantic_scholar_api_key: str | None = Field(default=None, alias="S2_API_KEY")
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

    # --- platform IAM: session auth for the dashboard/API ---
    app_base_url: str = "http://localhost:8000"  # backend (OAuth redirect target)
    frontend_base_url: str = "http://localhost:3000"  # where to send the user post-login
    auth_session_ttl_hours: int = 720  # 30 days
    auth_cookie_name: str = "aletheia_session"
    auth_cookie_secure: bool = False  # set true behind HTTPS in prod
    auth_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    # owner bootstrap: a local-password account seeded on first boot
    owner_email: str | None = Field(default=None, alias="ALETHEIA_OWNER_EMAIL")
    owner_password: str | None = Field(default=None, alias="ALETHEIA_OWNER_PASSWORD")
    # OAuth / phone provider credentials (real flows enabled when present)
    github_oauth_client_id: str | None = Field(default=None, alias="GITHUB_OAUTH_CLIENT_ID")
    github_oauth_client_secret: str | None = Field(default=None, alias="GITHUB_OAUTH_CLIENT_SECRET")
    feishu_app_id: str | None = Field(default=None, alias="FEISHU_APP_ID")
    feishu_app_secret: str | None = Field(default=None, alias="FEISHU_APP_SECRET")
    phone_otp_dev_mode: bool = True  # emit the OTP to logs instead of sending SMS
    sms_webhook_url: str | None = Field(default=None, alias="SMS_WEBHOOK_URL")
    # who may sign in via a not-yet-linked identity (email or phone, comma-sep).
    # empty => only the bootstrapped owner + already-linked identities + the very
    # first user may sign in (no open self-registration for a personal lab).
    auth_allowed_logins: str | None = Field(default=None, alias="ALETHEIA_ALLOWED_LOGINS")

    @property
    def critics(self) -> CriticsConfig:
        return CriticsConfig.load()

    @property
    def orchestrator_model(self) -> str:
        return self.openai_model if self.orchestrator_provider == "openai" else self.claude_model

    @property
    def orchestrator_transport(self) -> str:
        """Stable cache/provenance label for the selected provider and auth transport."""
        if self.orchestrator_provider == "openai":
            return "codex_cli" if self.openai_auth_mode == "subscription" else "responses_api"
        return "claude_agent_sdk"

    @property
    def orchestrator_vendor(self) -> str:
        return "openai" if self.orchestrator_provider == "openai" else "anthropic"

    @property
    def github_configured(self) -> bool:
        return bool(
            self.github_app_id
            and self.github_app_installation_id
            and (self.github_app_private_key or self.github_app_private_key_path)
        )

    @property
    def allowed_logins_set(self) -> set[str]:
        raw = self.auth_allowed_logins or ""
        return {x.strip().lower() for x in raw.split(",") if x.strip()}

    def auth_provider_enabled(self, provider: str) -> bool:
        """Whether a login provider has the config it needs to run real flows.
        local + phone(dev) are always available; OAuth needs client credentials."""
        if provider == "local":
            return True
        if provider == "github":
            return bool(self.github_oauth_client_id and self.github_oauth_client_secret)
        if provider == "feishu":
            return bool(self.feishu_app_id and self.feishu_app_secret)
        if provider == "phone":
            return self.phone_otp_dev_mode or bool(self.sms_webhook_url)
        return False

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
            # GLM: explicit ZHIPU_API_KEY wins; otherwise reuse the GLM Coding Plan key
            # already configured in OpenCode (no second place to keep the secret).
            "zhipu": self.zhipu_api_key or _opencode_glm_key(),
            # Grok/xAI: explicit GROK_API_KEY wins; else read it from the sibling sciminer
            # project's .env (where it's already configured).
            "grok": self.grok_api_key or _sciminer_grok_key(),
        }.get(vendor_id)

    def vendor_base_url(self, vendor_id: str) -> str | None:
        """Env override for an OpenAI-compatible vendor's endpoint (else None ->
        the provider falls back to the base_url in critics.yaml)."""
        return {
            "zhipu": self.zhipu_base_url,
            "deepseek": self.deepseek_base_url,
        }.get(vendor_id)


@functools.lru_cache
def _opencode_glm_key() -> str | None:
    """Read the GLM Coding Plan API key from OpenCode's config so the critic panel can
    reuse it without duplicating the secret. Looks for the ``zhipuai-coding-plan``
    provider in ``~/.config/opencode/opencode.json``. Best-effort: any error -> None."""
    import json as _json
    import os as _os
    from pathlib import Path as _Path

    base = _os.environ.get("XDG_CONFIG_HOME") or _Path.home() / ".config"
    path = _Path(base) / "opencode" / "opencode.json"
    try:
        data = _json.loads(path.read_text())
        opts = (data.get("provider", {}).get("zhipuai-coding-plan", {}) or {}).get("options", {})
        key = opts.get("apiKey")
        return str(key) if key else None
    except Exception:
        return None


@functools.lru_cache
def _sciminer_grok_key() -> str | None:
    """Read GROK_API_KEY from the sibling sciminer project's .env (``../sciminer/.env``)
    so the xAI/Grok key lives in one place. Override the path with SCIMINER_ENV.
    Best-effort: any error -> None."""
    import os as _os
    import re as _re
    from pathlib import Path as _Path

    override = _os.environ.get("SCIMINER_ENV")
    # settings.py -> .../aletheia/aletheia/config/settings.py; parents[3] = the dir that
    # holds both the aletheia repo and its sibling sciminer.
    path = _Path(override) if override else _Path(__file__).resolve().parents[3] / "sciminer" / ".env"
    try:
        m = _re.search(r"^GROK_API_KEY=(.+)$", _Path(path).read_text(), _re.M)
        return m.group(1).strip().strip('"').strip("'") if m else None
    except Exception:
        return None


@functools.lru_cache
def get_settings() -> Settings:
    return Settings()
