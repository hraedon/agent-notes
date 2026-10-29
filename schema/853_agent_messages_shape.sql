-- Plan 025: bound what a message row can hold (review fixes, round 2).
--
-- * subject: at most 512 characters. Hooks render a subject inside a ~8 KB
--   injection budget; an unbounded one could crowd out its own message. The
--   body already has its own 32 KB CHECK (851).
-- * payload: a JSON object. The dispatcher and `review show` read named keys
--   from it; any other JSON type is a malformed request.
--
-- Existing rows are brought into shape first, because a CHECK also applies to
-- the UPDATE that claims a row (NOT VALID would leave old rows unclaimable): a
-- long subject is cut, and a non-object payload is kept under
-- `invalid_payload` (a request without `job` is answered BAD_REQUEST).
--
-- Idempotent: safe to re-run.

UPDATE agent_messages
SET subject = left(subject, 509) || '...'
WHERE length(subject) > 512;

UPDATE agent_messages
SET payload = jsonb_build_object('invalid_payload', payload)
WHERE jsonb_typeof(payload) <> 'object';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'agent_messages_subject_length'
          AND conrelid = 'agent_messages'::regclass
    ) THEN
        ALTER TABLE agent_messages
            ADD CONSTRAINT agent_messages_subject_length
            CHECK (subject IS NULL OR length(subject) <= 512);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'agent_messages_payload_object'
          AND conrelid = 'agent_messages'::regclass
    ) THEN
        ALTER TABLE agent_messages
            ADD CONSTRAINT agent_messages_payload_object
            CHECK (jsonb_typeof(payload) = 'object');
    END IF;
END
$$;
