DELETE FROM ai_plan_steps;
DELETE FROM ai_plan_days;
DELETE FROM ai_plans;

ALTER TABLE ai_plans ADD COLUMN IF NOT EXISTS current_mode TEXT DEFAULT 'standard';
ALTER TABLE ai_plans ADD COLUMN IF NOT EXISTS milestone_status TEXT DEFAULT 'pending';
