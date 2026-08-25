CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE critique_panels (
    id VARCHAR(32) NOT NULL,
    target VARCHAR(32) NOT NULL,
    target_ref VARCHAR(32),
    consensus_verdict VARCHAR(32),
    gate_passed BOOLEAN,
    raw_json JSONB,
    ts TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id)
);

CREATE TABLE events (
    id BIGSERIAL NOT NULL,
    run_id VARCHAR(32),
    agent VARCHAR(64),
    parent_tool_use_id VARCHAR(128),
    type VARCHAR(64) NOT NULL,
    payload JSONB,
    ts TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id)
);
CREATE INDEX ix_events_run_id ON events (run_id);
CREATE INDEX ix_events_run_ts ON events (run_id, ts);
CREATE INDEX ix_events_type ON events (type);

CREATE TABLE memory_chunks (
    id BIGSERIAL NOT NULL,
    run_id VARCHAR(32),
    experiment_id VARCHAR(32),
    kind VARCHAR(32) NOT NULL,
    text TEXT NOT NULL,
    embedding VECTOR(384) NOT NULL,
    meta_json JSONB,
    ts TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id)
);
CREATE INDEX ix_memory_chunks_kind ON memory_chunks (kind);
CREATE INDEX ix_memory_chunks_run_id ON memory_chunks (run_id);

CREATE TABLE runs (
    id VARCHAR(32) NOT NULL,
    domain VARCHAR(128),
    direction TEXT,
    goal TEXT,
    status VARCHAR(32) NOT NULL,
    human_owner VARCHAR(128),
    budget_cap_usd FLOAT,
    gpu_hours_cap FLOAT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id)
);

CREATE TABLE users (
    id VARCHAR(32) NOT NULL,
    display_name VARCHAR(128),
    email VARCHAR(256),
    role VARCHAR(16) NOT NULL,
    is_active BOOLEAN NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id)
);
CREATE INDEX ix_users_email ON users (email);

CREATE TABLE auth_sessions (
    id VARCHAR(32) NOT NULL,
    user_id VARCHAR(32) NOT NULL,
    token_hash VARCHAR(64) NOT NULL,
    revoked BOOLEAN NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(user_id) REFERENCES users (id)
);
CREATE UNIQUE INDEX ix_auth_sessions_token_hash ON auth_sessions (token_hash);
CREATE INDEX ix_auth_sessions_user_id ON auth_sessions (user_id);

CREATE TABLE belief_states (
    id VARCHAR(32) NOT NULL,
    run_id VARCHAR(32) NOT NULL,
    question_key VARCHAR(96) NOT NULL,
    alpha FLOAT NOT NULL,
    beta FLOAT NOT NULL,
    n_updates INTEGER NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_belief_run_question UNIQUE (run_id, question_key),
    FOREIGN KEY(run_id) REFERENCES runs (id)
);
CREATE INDEX ix_belief_states_question_key ON belief_states (question_key);
CREATE INDEX ix_belief_states_run_id ON belief_states (run_id);

CREATE TABLE budget_events (
    id BIGSERIAL NOT NULL,
    run_id VARCHAR(32) NOT NULL,
    kind VARCHAR(32) NOT NULL,
    amount FLOAT NOT NULL,
    cumulative FLOAT,
    ts TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(run_id) REFERENCES runs (id)
);
CREATE INDEX ix_budget_events_run_id ON budget_events (run_id);

CREATE TABLE campaign_split_ledgers (
    id VARCHAR(32) NOT NULL,
    run_id VARCHAR(32) NOT NULL,
    dataset_fingerprint VARCHAR(64) NOT NULL,
    row_identity_hash VARCHAR(64) NOT NULL,
    split_algo_version INTEGER NOT NULL,
    state VARCHAR(24) NOT NULL,
    plan_json JSONB NOT NULL,
    final_result_json JSONB,
    final_opened_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_campaign_split_run UNIQUE (run_id),
    FOREIGN KEY(run_id) REFERENCES runs (id)
);
CREATE INDEX ix_campaign_split_ledgers_run_id ON campaign_split_ledgers (run_id);

CREATE TABLE data_assets (
    id VARCHAR(32) NOT NULL,
    run_id VARCHAR(32) NOT NULL,
    role VARCHAR(32) NOT NULL,
    source VARCHAR(16) NOT NULL,
    ref TEXT,
    target_column VARCHAR(128),
    composition_column VARCHAR(128),
    feature_kind VARCHAR(32),
    description TEXT,
    status VARCHAR(16) NOT NULL,
    uri TEXT,
    content_sha256 VARCHAR(64),
    profile_json JSONB,
    requested_by VARCHAR(16) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(run_id) REFERENCES runs (id)
);
CREATE INDEX ix_data_assets_run_id ON data_assets (run_id);

CREATE TABLE experiments (
    id VARCHAR(32) NOT NULL,
    run_id VARCHAR(32) NOT NULL,
    hypothesis TEXT,
    design_json JSONB,
    stage VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL,
    code_repo VARCHAR(256),
    code_branch VARCHAR(256),
    parent_experiment_id VARCHAR(32),
    dedup_key VARCHAR(64),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    finished_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (id),
    FOREIGN KEY(run_id) REFERENCES runs (id),
    FOREIGN KEY(parent_experiment_id) REFERENCES experiments (id)
);
CREATE INDEX ix_experiments_dedup_key ON experiments (dedup_key);
CREATE INDEX ix_experiments_run_id ON experiments (run_id);

CREATE TABLE identities (
    id BIGSERIAL NOT NULL,
    user_id VARCHAR(32) NOT NULL,
    provider VARCHAR(16) NOT NULL,
    subject VARCHAR(256) NOT NULL,
    secret_hash TEXT,
    meta_json JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_identity_provider_subject UNIQUE (provider, subject),
    FOREIGN KEY(user_id) REFERENCES users (id)
);
CREATE INDEX ix_identities_user_id ON identities (user_id);

CREATE TABLE literature_findings (
    id BIGSERIAL NOT NULL,
    run_id VARCHAR(32) NOT NULL,
    paper_id VARCHAR(256),
    query TEXT,
    method TEXT,
    dataset TEXT,
    metric VARCHAR(128),
    result TEXT,
    limitation TEXT,
    gap TEXT,
    relevance TEXT,
    source VARCHAR(64),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(run_id) REFERENCES runs (id)
);
CREATE INDEX ix_literature_findings_run_id ON literature_findings (run_id);

CREATE TABLE sota_results (
    id BIGSERIAL NOT NULL,
    run_id VARCHAR(32) NOT NULL,
    domain VARCHAR(128),
    task TEXT,
    dataset TEXT,
    metric VARCHAR(128),
    score FLOAT,
    method TEXT,
    source TEXT,
    split_policy VARCHAR(128),
    notes TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(run_id) REFERENCES runs (id)
);
CREATE INDEX ix_sota_results_domain ON sota_results (domain);
CREATE INDEX ix_sota_results_run_id ON sota_results (run_id);

CREATE TABLE worker_cache (
    id BIGSERIAL NOT NULL,
    run_id VARCHAR(32) NOT NULL,
    cache_key VARCHAR(64) NOT NULL,
    label VARCHAR(128),
    result TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_worker_cache_run_key UNIQUE (run_id, cache_key),
    FOREIGN KEY(run_id) REFERENCES runs (id)
);
CREATE INDEX ix_worker_cache_run_id ON worker_cache (run_id);

CREATE TABLE artifacts (
    id BIGSERIAL NOT NULL,
    experiment_id VARCHAR(32) NOT NULL,
    kind VARCHAR(64) NOT NULL,
    uri TEXT NOT NULL,
    sha256 VARCHAR(64),
    bytes BIGINT,
    ts TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(experiment_id) REFERENCES experiments (id)
);
CREATE INDEX ix_artifacts_experiment_id ON artifacts (experiment_id);

CREATE TABLE claims (
    id VARCHAR(32) NOT NULL,
    run_id VARCHAR(32) NOT NULL,
    experiment_id VARCHAR(32),
    claim_text TEXT NOT NULL,
    claim_type VARCHAR(32) NOT NULL,
    strength VARCHAR(16) NOT NULL,
    status VARCHAR(16) NOT NULL,
    created_by VARCHAR(64),
    stage VARCHAR(32),
    dedup_key VARCHAR(64),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(run_id) REFERENCES runs (id),
    FOREIGN KEY(experiment_id) REFERENCES experiments (id)
);
CREATE INDEX ix_claims_dedup_key ON claims (dedup_key);
CREATE INDEX ix_claims_experiment_id ON claims (experiment_id);
CREATE INDEX ix_claims_run_id ON claims (run_id);

CREATE TABLE compute_jobs (
    id VARCHAR(32) NOT NULL,
    experiment_id VARCHAR(32),
    backend VARCHAR(32) NOT NULL,
    ext_id VARCHAR(128),
    status VARCHAR(32) NOT NULL,
    resources_json JSONB,
    ts TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(experiment_id) REFERENCES experiments (id)
);
CREATE INDEX ix_compute_jobs_experiment_id ON compute_jobs (experiment_id);

CREATE TABLE decisions (
    id BIGSERIAL NOT NULL,
    run_id VARCHAR(32) NOT NULL,
    experiment_id VARCHAR(32),
    stage_from VARCHAR(32),
    stage_to VARCHAR(32),
    rationale TEXT,
    actor VARCHAR(64),
    critique_panel_id VARCHAR(32),
    ts TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(run_id) REFERENCES runs (id),
    FOREIGN KEY(experiment_id) REFERENCES experiments (id),
    FOREIGN KEY(critique_panel_id) REFERENCES critique_panels (id)
);
CREATE INDEX ix_decisions_run_id ON decisions (run_id);

CREATE TABLE external_validation_ledgers (
    id VARCHAR(32) NOT NULL,
    run_id VARCHAR(32) NOT NULL,
    data_asset_id VARCHAR(32) NOT NULL,
    dataset_fingerprint VARCHAR(64) NOT NULL,
    row_identity_hash VARCHAR(64) NOT NULL,
    state VARCHAR(24) NOT NULL,
    provenance_json JSONB NOT NULL,
    result_json JSONB,
    opened_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_external_validation_run UNIQUE (run_id),
    FOREIGN KEY(run_id) REFERENCES runs (id),
    FOREIGN KEY(data_asset_id) REFERENCES data_assets (id)
);
CREATE INDEX ix_external_validation_ledgers_data_asset_id
    ON external_validation_ledgers (data_asset_id);
CREATE INDEX ix_external_validation_ledgers_run_id ON external_validation_ledgers (run_id);

CREATE TABLE hypothesis_attempts (
    id BIGSERIAL NOT NULL,
    run_id VARCHAR(32) NOT NULL,
    experiment_id VARCHAR(32),
    family_key VARCHAR(64) NOT NULL,
    hypothesis_key VARCHAR(64) NOT NULL,
    hypothesis_text TEXT NOT NULL,
    round_index INTEGER NOT NULL,
    phase VARCHAR(24) NOT NULL,
    confirmation_batch INTEGER,
    split_hash VARCHAR(64) NOT NULL,
    alpha_allocated FLOAT NOT NULL,
    status VARCHAR(24) NOT NULL,
    outcome_json JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_hypothesis_attempt_run_exp_phase UNIQUE (run_id, experiment_id, phase),
    FOREIGN KEY(run_id) REFERENCES runs (id),
    FOREIGN KEY(experiment_id) REFERENCES experiments (id)
);
CREATE INDEX ix_hypothesis_attempts_experiment_id ON hypothesis_attempts (experiment_id);
CREATE INDEX ix_hypothesis_attempts_family_key ON hypothesis_attempts (family_key);
CREATE INDEX ix_hypothesis_attempts_run_id ON hypothesis_attempts (run_id);

CREATE TABLE hypothesis_scorecards (
    id VARCHAR(32) NOT NULL,
    run_id VARCHAR(32) NOT NULL,
    experiment_id VARCHAR(32),
    scores JSONB,
    decision VARCHAR(16),
    rationale TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(run_id) REFERENCES runs (id),
    FOREIGN KEY(experiment_id) REFERENCES experiments (id)
);
CREATE INDEX ix_hypothesis_scorecards_experiment_id ON hypothesis_scorecards (experiment_id);
CREATE INDEX ix_hypothesis_scorecards_run_id ON hypothesis_scorecards (run_id);

CREATE TABLE metrics (
    id BIGSERIAL NOT NULL,
    experiment_id VARCHAR(32) NOT NULL,
    name VARCHAR(128) NOT NULL,
    value FLOAT NOT NULL,
    split VARCHAR(32),
    step BIGINT,
    ts TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(experiment_id) REFERENCES experiments (id)
);
CREATE INDEX ix_metrics_experiment_id ON metrics (experiment_id);

CREATE TABLE claim_evidence (
    id BIGSERIAL NOT NULL,
    claim_id VARCHAR(32) NOT NULL,
    evidence_kind VARCHAR(32) NOT NULL,
    evidence_ref TEXT NOT NULL,
    note TEXT,
    PRIMARY KEY (id),
    FOREIGN KEY(claim_id) REFERENCES claims (id)
);
CREATE INDEX ix_claim_evidence_claim_id ON claim_evidence (claim_id);
