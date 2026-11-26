-- Миграция 0007: настройки уведомлений, выбор города и очередь доставки

CREATE TABLE IF NOT EXISTS notification_settings (
    user_id BIGINT PRIMARY KEY,
    destination_chat_id BIGINT NULL,
    city_name TEXT NULL,
    city_country TEXT NULL,
    city_region TEXT NULL,
    city_latitude DOUBLE PRECISION NULL,
    city_longitude DOUBLE PRECISION NULL,
    city_confirmed_at TIMESTAMPTZ NULL,
    briefing_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    briefing_consent_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT notification_settings_city_coordinates CHECK (
        (city_latitude IS NULL) = (city_longitude IS NULL)
    ),
    CONSTRAINT notification_settings_latitude CHECK (
        city_latitude IS NULL OR city_latitude BETWEEN -90 AND 90
    ),
    CONSTRAINT notification_settings_longitude CHECK (
        city_longitude IS NULL OR city_longitude BETWEEN -180 AND 180
    )
);

CREATE TABLE IF NOT EXISTS notification_city_selections (
    id UUID PRIMARY KEY,
    user_id BIGINT NOT NULL,
    candidates JSONB NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (
        state IN ('pending', 'confirmed', 'cancelled', 'expired')
    ),
    selected_index INT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    confirmed_at TIMESTAMPTZ NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT notification_city_selections_index CHECK (
        selected_index IS NULL OR selected_index >= 0
    )
);

CREATE INDEX IF NOT EXISTS idx_notification_city_selections_user_state
ON notification_city_selections (user_id, state, expires_at);

CREATE TABLE IF NOT EXISTS outgoing_notifications (
    id UUID PRIMARY KEY,
    event_key TEXT NOT NULL UNIQUE,
    user_id BIGINT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('reminder', 'daily_briefing')),
    destination_chat_id BIGINT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (
        state IN ('pending', 'sending', 'retry', 'sent', 'failed', 'missed', 'suppressed')
    ),
    attempt_count INT NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    scheduled_for TIMESTAMPTZ NOT NULL,
    next_attempt_at TIMESTAMPTZ NOT NULL,
    lease_id UUID NULL,
    lease_until TIMESTAMPTZ NULL,
    last_error_code TEXT NULL,
    trace_id UUID NOT NULL,
    sent_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_outgoing_notifications_due
ON outgoing_notifications (state, next_attempt_at, created_at);

CREATE INDEX IF NOT EXISTS idx_outgoing_notifications_user_created
ON outgoing_notifications (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_outgoing_notifications_lease
ON outgoing_notifications (state, lease_until);
