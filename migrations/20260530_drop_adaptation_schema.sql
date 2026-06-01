-- T5.4: Remove adaptation schema.
-- Adaptations are fully removed from application code.

-- 1. Migrate canceled_by_adaptation steps to 'canceled' (not 'expired').
--    'expired' = user missed a delivery window → counted in silent_miss_rate.
--    'canceled' = system-removed step → excluded from completion metrics.
--    Using 'expired' here would inflate churn signals for users who had adapted plans.
UPDATE ai_plan_steps
    SET step_status = 'canceled'
    WHERE canceled_by_adaptation = TRUE
      AND step_status NOT IN ('completed', 'skipped', 'expired', 'canceled');

-- 2. Drop adaptation_history table (no runtime writers since T5.4)
DROP TABLE IF EXISTS adaptation_history CASCADE;

-- 3. Drop canceled_by_adaptation column from ai_plan_steps
ALTER TABLE ai_plan_steps
    DROP COLUMN IF EXISTS canceled_by_adaptation;

-- 4. Drop adaptation_version from ai_plans
ALTER TABLE ai_plans
    DROP COLUMN IF EXISTS adaptation_version;

-- 5. Drop adaptation_requests_count from user_profiles
ALTER TABLE user_profiles
    DROP COLUMN IF EXISTS adaptation_requests_count;
