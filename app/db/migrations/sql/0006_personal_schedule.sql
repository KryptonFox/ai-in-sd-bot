-- Миграция 0006: личное расписание из iCalendar

CREATE TABLE personal_calendars (
    user_id BIGINT PRIMARY KEY REFERENCES user_settings(user_id) ON DELETE CASCADE,
    timezone TEXT NOT NULL,
    source_name TEXT NOT NULL CHECK (length(source_name) BETWEEN 1 AND 255),
    imported_at TIMESTAMPTZ NOT NULL,
    event_count INT NOT NULL CHECK (event_count >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE personal_calendar_events (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES personal_calendars(user_id) ON DELETE CASCADE,
    uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 1024),
    recurrence_id TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL CHECK (length(summary) BETWEEN 1 AND 1000),
    location TEXT NOT NULL DEFAULT '' CHECK (length(location) <= 1000),
    starts_at TIMESTAMPTZ NOT NULL,
    ends_at TIMESTAMPTZ NOT NULL,
    all_day BOOLEAN NOT NULL DEFAULT FALSE,
    cancelled BOOLEAN NOT NULL DEFAULT FALSE,
    timezone TEXT NOT NULL,
    rrule TEXT NULL,
    rdate TIMESTAMPTZ[] NOT NULL DEFAULT '{}',
    exdate TIMESTAMPTZ[] NOT NULL DEFAULT '{}',
    UNIQUE (user_id, uid, recurrence_id),
    CHECK (ends_at > starts_at)
);

CREATE INDEX idx_personal_calendar_events_user_start
ON personal_calendar_events (user_id, starts_at);
