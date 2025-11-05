BEGIN;
ALTER TABLE ai_plans ADD COLUMN approved_at TIMESTAMP;
ALTER TABLE ai_plans ALTER COLUMN status SET DEFAULT 'draft';
UPDATE ai_plans SET status='active' WHERE status IS NULL;
ALTER TABLE ai_plan_steps ADD COLUMN status VARCHAR DEFAULT 'pending';
ALTER TABLE ai_plan_steps ADD COLUMN proposed_for TIMESTAMP;
UPDATE ai_plan_steps SET status='approved' WHERE job_id IS NOT NULL;
COMMIT;
