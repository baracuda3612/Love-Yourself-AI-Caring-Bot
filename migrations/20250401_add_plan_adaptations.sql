BEGIN;

ALTER TABLE ai_plans
    ADD COLUMN IF NOT EXISTS execution_policy VARCHAR NOT NULL DEFAULT 'active';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_ai_plans_execution_policy'
    ) THEN
        ALTER TABLE ai_plans
            ADD CONSTRAINT ck_ai_plans_execution_policy
            CHECK (execution_policy IN ('active', 'paused'));
    END IF;
END$$;

CREATE TABLE IF NOT EXISTS ai_plan_versions (
    id SERIAL PRIMARY KEY,
    plan_id INTEGER NOT NULL REFERENCES ai_plans(id) ON DELETE CASCADE,
    applied_adaptation_type VARCHAR NOT NULL,
    diff JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ai_plan_versions_plan_id ON ai_plan_versions (plan_id);

COMMIT;
