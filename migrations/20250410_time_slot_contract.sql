BEGIN;

ALTER TABLE user_profiles
    ADD COLUMN IF NOT EXISTS daily_time_slots JSONB NOT NULL
    DEFAULT '{"MORNING":"09:30","DAY":"14:00","EVENING":"21:00"}'::jsonb;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'ai_plan_steps'
          AND column_name = 'time_of_day'
    ) THEN
        ALTER TABLE ai_plan_steps
            RENAME COLUMN time_of_day TO time_slot;
    END IF;
END$$;

ALTER TABLE ai_plan_steps
    ADD COLUMN IF NOT EXISTS time_slot VARCHAR;

ALTER TABLE ai_plan_steps
    ALTER COLUMN time_slot SET DEFAULT 'DAY';

UPDATE ai_plan_steps
SET time_slot = CASE
    WHEN time_slot IS NULL THEN 'DAY'
    WHEN LOWER(time_slot) = 'morning' THEN 'MORNING'
    WHEN LOWER(time_slot) IN ('afternoon', 'day') THEN 'DAY'
    WHEN LOWER(time_slot) IN ('evening', 'night') THEN 'EVENING'
    WHEN LOWER(time_slot) = 'any' THEN 'DAY'
    ELSE 'DAY'
END;

ALTER TABLE ai_plan_steps
    ALTER COLUMN time_slot SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_ai_plan_steps_time_slot'
    ) THEN
        ALTER TABLE ai_plan_steps
            ADD CONSTRAINT ck_ai_plan_steps_time_slot
            CHECK (time_slot IN ('MORNING', 'DAY', 'EVENING'));
    END IF;
END$$;

COMMIT;
