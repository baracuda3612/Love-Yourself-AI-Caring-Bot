BEGIN;

UPDATE ai_plans
SET status = 'active'
WHERE status = 'paused';

COMMIT;
