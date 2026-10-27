ALTER TABLE dialog_messages ADD COLUMN event_id TEXT NULL;

CREATE UNIQUE INDEX idx_dialog_messages_event_role
ON dialog_messages (user_id, event_id, role)
WHERE event_id IS NOT NULL;
