ALTER TABLE agent_events
DROP CONSTRAINT IF EXISTS agent_events_calls_used_check;

ALTER TABLE agent_events
ADD CONSTRAINT agent_events_calls_used_nonnegative CHECK (calls_used >= 0);

ALTER TABLE pending_confirmations
DROP CONSTRAINT IF EXISTS pending_confirmations_calls_remaining_check;

ALTER TABLE pending_confirmations
ALTER COLUMN calls_remaining DROP DEFAULT;

ALTER TABLE pending_confirmations
ADD CONSTRAINT pending_confirmations_calls_remaining_nonnegative
CHECK (calls_remaining >= 0);
