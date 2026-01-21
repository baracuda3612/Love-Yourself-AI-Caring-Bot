BEGIN;

ALTER TABLE ai_plan_steps
    ADD COLUMN IF NOT EXISTS exercise_id VARCHAR;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_ai_plan_steps_exercise_id'
    ) THEN
        ALTER TABLE ai_plan_steps
            ADD CONSTRAINT fk_ai_plan_steps_exercise_id
            FOREIGN KEY (exercise_id)
            REFERENCES content_library(id)
            ON DELETE SET NULL;
    END IF;
END$$;

CREATE INDEX IF NOT EXISTS idx_ai_plan_steps_exercise_id ON ai_plan_steps (exercise_id);

COMMIT;
