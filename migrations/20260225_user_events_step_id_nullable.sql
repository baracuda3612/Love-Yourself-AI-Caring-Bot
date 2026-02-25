-- Make user_events.step_id nullable for plan-level telemetry events.

BEGIN;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'user_events'
          AND column_name = 'step_id'
          AND is_nullable = 'NO'
    ) THEN
        ALTER TABLE user_events ALTER COLUMN step_id DROP NOT NULL;
    END IF;
END
$$;

COMMIT;
