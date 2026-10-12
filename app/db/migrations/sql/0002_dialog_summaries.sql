-- Миграция 0002: Таблица для хранения саммари диалогов при компактизации

CREATE TABLE IF NOT EXISTS dialog_summaries (
    user_id BIGINT PRIMARY KEY REFERENCES user_settings(user_id) ON DELETE CASCADE,
    summary TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
