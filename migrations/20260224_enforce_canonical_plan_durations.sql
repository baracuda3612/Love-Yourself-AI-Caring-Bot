-- Canonical plan durations: 7, 14, 21, 90.

BEGIN;

UPDATE ai_plans
SET total_days = 7
WHERE total_days = 10;

UPDATE plan_drafts
SET total_days = 7
WHERE total_days = 10;

ALTER TABLE ai_plans
    DROP CONSTRAINT IF EXISTS ai_plans_total_days_canonical_check;

ALTER TABLE ai_plans
    ADD CONSTRAINT ai_plans_total_days_canonical_check
    CHECK (total_days IS NULL OR total_days IN (7, 14, 21, 90));

ALTER TABLE plan_drafts
    DROP CONSTRAINT IF EXISTS plan_drafts_total_days_canonical_check;

ALTER TABLE plan_drafts
    ADD CONSTRAINT plan_drafts_total_days_canonical_check
    CHECK (total_days IN (7, 14, 21, 90));

COMMIT;
