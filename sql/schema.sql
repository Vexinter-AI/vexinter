-- Baseline schema (idempotent). Fresh installs run this file and are
-- recorded at SCHEMA_VERSION. Changes that old databases cannot
-- tolerate go BOTH here (for fresh installs) and in
-- sql/migrations/<version>_<slug>.sql (for upgrades); bump
-- SCHEMA_VERSION in vexinter/store_pg.py with each migration.

CREATE TABLE IF NOT EXISTS vex_event (
    id           TEXT PRIMARY KEY,
    stream       TEXT        NOT NULL,
    seq          BIGINT      NOT NULL,
    ts           TIMESTAMPTZ NOT NULL,
    type         TEXT        NOT NULL,
    task_id      TEXT,
    execution_id TEXT,
    agent_id     TEXT,
    operation_id TEXT,
    caused_by    TEXT,
    depends_on   JSONB NOT NULL DEFAULT '[]'::jsonb,
    compensates  TEXT,
    payload      JSONB,
    payload_ref  TEXT,
    payload_hash TEXT NOT NULL,
    prev_hash    TEXT NOT NULL,
    this_hash    TEXT NOT NULL,
    signature    TEXT,
    UNIQUE (stream, seq)                 
);
CREATE INDEX IF NOT EXISTS idx_vex_event_task   ON vex_event (task_id);
CREATE INDEX IF NOT EXISTS idx_vex_event_exec  ON vex_event (execution_id);
CREATE INDEX IF NOT EXISTS idx_vex_event_type  ON vex_event (type, ts);
CREATE INDEX IF NOT EXISTS idx_vex_event_stream ON vex_event (stream, seq);
CREATE INDEX IF NOT EXISTS idx_vex_event_exec_ts ON vex_event (execution_id, ts, seq);
CREATE INDEX IF NOT EXISTS idx_vex_event_ts_seq  ON vex_event (ts, seq);
CREATE INDEX IF NOT EXISTS idx_vex_msg_to
    ON vex_event ((payload->>'to')) WHERE type = 'message.sent';

CREATE TABLE IF NOT EXISTS vex_stream_head (
    stream    TEXT PRIMARY KEY,
    seq       BIGINT NOT NULL,
    head_hash TEXT  NOT NULL
);

CREATE TABLE IF NOT EXISTS vex_checkpoint (
    id        TEXT PRIMARY KEY,
    ts        TIMESTAMPTZ NOT NULL,
    heads     JSONB NOT NULL,
    head_hash TEXT  NOT NULL,
    signature TEXT,
    signer    TEXT
);


CREATE TABLE IF NOT EXISTS vex_api_seq (
    k        TEXT PRIMARY KEY,
    last_seq BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS vex_api_nonce (
    k       TEXT        NOT NULL,
    seq     BIGINT      NOT NULL,
    seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (k, seq)
);
CREATE INDEX IF NOT EXISTS idx_vex_api_nonce_seen ON vex_api_nonce (seen_at);
CREATE TABLE IF NOT EXISTS vex_schema_migrations (
    version    INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS vex_tenant (
    tenant_id  TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    tier       TEXT NOT NULL,
    status     TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ,
    settings   JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata   JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS vex_tenant_member (
    member_id  TEXT PRIMARY KEY,
    tenant_id  TEXT NOT NULL REFERENCES vex_tenant(tenant_id) ON DELETE CASCADE,
    user_id    TEXT NOT NULL,
    roles      JSONB NOT NULL DEFAULT '[]'::jsonb,
    joined_at  TIMESTAMPTZ NOT NULL,
    invited_by TEXT,
    is_active  BOOLEAN NOT NULL DEFAULT true,
    UNIQUE (tenant_id, user_id)
);

CREATE TABLE IF NOT EXISTS vex_tenant_agent (
    agent_id  TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES vex_tenant(tenant_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS vex_tenant_usage (
    tenant_id TEXT NOT NULL REFERENCES vex_tenant(tenant_id) ON DELETE CASCADE,
    day       DATE NOT NULL,
    count     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, day)
);

CREATE TABLE IF NOT EXISTS vex_tier_usage (
    email         TEXT NOT NULL,
    tier          TEXT NOT NULL,
    period_start  DATE NOT NULL,
    tasks_used    INTEGER NOT NULL DEFAULT 0,
    api_calls_used INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (email, tier, period_start)
);

CREATE INDEX IF NOT EXISTS idx_vex_tenant_agent_tenant ON vex_tenant_agent (tenant_id);
