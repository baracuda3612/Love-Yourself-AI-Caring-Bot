-- T5.2: Plan Runtime Migration
-- Connects PlanBuilderV5 to production flow.
-- Run after T5.1 migration (20260525_plan_task_model_v51.sql).

BEGIN;

-- 1. plan_drafts: make focus/load nullable
--    v5 plans do not use focus/load concepts.
ALTER TABLE plan_drafts
    ALTER COLUMN focus DROP NOT NULL,
    ALTER COLUMN load  DROP NOT NULL;

-- 2. plan_draft_steps: add mechanic column
--    Stores the mechanic snapshot per step so finalize_plan can read it.
--    Default 'switch' covers legacy draft rows created before T5.2.
ALTER TABLE plan_draft_steps
    ADD COLUMN IF NOT EXISTS mechanic VARCHAR(10) DEFAULT 'switch'
        CHECK (mechanic IN ('switch', 'unload'));

-- 3. plan_draft_steps: make legacy columns nullable
--    slot_type, category, difficulty are not used in v5 drafts.
ALTER TABLE plan_draft_steps
    ALTER COLUMN slot_type  DROP NOT NULL,
    ALTER COLUMN category   DROP NOT NULL,
    ALTER COLUMN difficulty DROP NOT NULL;

-- 4. ai_plans: make load nullable
--    v5 plans have no load concept. finalize_plan() sets plan.load = draft.load = NULL.
--    Without this, db.flush() raises NOT NULL violation on every v5 plan creation.
ALTER TABLE ai_plans
    ALTER COLUMN load DROP NOT NULL;

-- 5. ai_plan_steps: remove DEFAULT from mechanic
--    After T5.2, every INSERT must explicitly provide mechanic.
--    Missing mechanic = DB error, not silent 'switch'. Safety net.
ALTER TABLE ai_plan_steps
    ALTER COLUMN mechanic DROP DEFAULT;

COMMIT;

-- Rollback:
-- BEGIN;
-- ALTER TABLE plan_drafts ALTER COLUMN focus SET NOT NULL, ALTER COLUMN load SET NOT NULL;
-- ALTER TABLE plan_draft_steps DROP COLUMN IF EXISTS mechanic;
-- ALTER TABLE plan_draft_steps ALTER COLUMN slot_type SET NOT NULL, ALTER COLUMN category SET NOT NULL, ALTER COLUMN difficulty SET NOT NULL;
-- ALTER TABLE ai_plans ALTER COLUMN load SET NOT NULL;
-- ALTER TABLE ai_plan_steps ALTER COLUMN mechanic SET DEFAULT 'switch';
-- COMMIT;
