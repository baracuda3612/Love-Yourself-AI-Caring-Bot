-- 1. Safety check: ensure no active plans have NULL load
SELECT id, user_id
FROM ai_plans
WHERE status = 'active' AND load IS NULL;

-- If any rows returned → STOP and fix manually.

-- 2. Safety check: ensure no plans at all have NULL load
SELECT id
FROM ai_plans
WHERE load IS NULL;

-- If any rows returned → STOP and backfill manually.

-- 3. Enforce NOT NULL constraint
ALTER TABLE ai_plans
ALTER COLUMN load SET NOT NULL;
