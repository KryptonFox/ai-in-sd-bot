ALTER TABLE user_settings
ADD COLUMN timezone TEXT NULL;

CREATE TABLE reminders (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    text TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 500),
    remind_at TIMESTAMPTZ NOT NULL,
    timezone TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_reminders_user_created ON reminders (user_id, created_at);

CREATE TABLE pending_confirmations (
    id UUID PRIMARY KEY,
    user_id BIGINT NOT NULL,
    event_id TEXT NOT NULL,
    position INT NOT NULL,
    tool_name TEXT NOT NULL DEFAULT 'add_reminder',
    arguments JSONB NOT NULL,
    timezone TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (
        state IN ('pending', 'confirmed', 'executing', 'succeeded', 'cancelled', 'expired', 'failed')
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    confirmed_at TIMESTAMPTZ NULL,
    execution_started_at TIMESTAMPTZ NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    calls_remaining INT NOT NULL DEFAULT 2 CHECK (calls_remaining BETWEEN 0 AND 2),
    request_id TEXT NOT NULL,
    result JSONB NULL,
    error_code TEXT NULL,
    UNIQUE (user_id, event_id, position)
);

CREATE INDEX idx_pending_confirmations_user_created
ON pending_confirmations (user_id, created_at);
CREATE INDEX idx_pending_confirmations_state_updated
ON pending_confirmations (state, updated_at);

CREATE TABLE decision_audits (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    event_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    tool_name TEXT NULL,
    arguments JSONB NOT NULL DEFAULT '{}'::jsonb,
    validation TEXT NOT NULL,
    execution TEXT NOT NULL,
    duration_ms DOUBLE PRECISION NOT NULL DEFAULT 0,
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_decision_audits_user_id_desc
ON decision_audits (user_id, id DESC);

CREATE TABLE agent_events (
    user_id BIGINT NOT NULL,
    event_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'processing',
    calls_used INT NOT NULL DEFAULT 0 CHECK (calls_used BETWEEN 0 AND 2),
    result JSONB NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, event_id)
);
