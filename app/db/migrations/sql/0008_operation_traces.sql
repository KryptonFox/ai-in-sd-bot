CREATE TABLE operation_traces (
    id UUID PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES user_settings(user_id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    route TEXT
);

CREATE INDEX idx_operation_traces_user_started
    ON operation_traces (user_id, started_at DESC, id DESC);
CREATE INDEX idx_operation_traces_started ON operation_traces (started_at);

CREATE TABLE operation_trace_steps (
    id UUID PRIMARY KEY,
    trace_id UUID NOT NULL REFERENCES operation_traces(id) ON DELETE CASCADE,
    parent_id UUID,
    position INTEGER NOT NULL CHECK (position >= 0),
    component TEXT NOT NULL,
    action TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    duration_ms DOUBLE PRECISION NOT NULL CHECK (duration_ms >= 0),
    status TEXT NOT NULL,
    tool_name TEXT,
    model_name TEXT,
    input_tokens BIGINT,
    output_tokens BIGINT,
    error_code TEXT,
    UNIQUE (trace_id, position)
);
