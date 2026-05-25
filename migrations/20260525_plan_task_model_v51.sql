-- T5.1 Plan & Task Model Overhaul
-- Ref: product_internal_spec.md v2.0
--
-- Adds:
--   ai_plan_steps.mechanic            — mechanic snapshot per step
--   user_profiles.is_paused           — simple pause flag (no plan rewrite)
--   user_profiles.pause_count         — cumulative pause counter (analytics)
--   user_profiles.evening_slot_collected — MEDIUM evening-slot collected flag

BEGIN;

-- ── ai_plan_steps ─────────────────────────────────────────────────────────────
-- mechanic: snapshot of exercise.mechanic at plan generation time.
-- Invariant (T5.1): never recomputed at delivery — snapshotted once at build time.
--
-- DEFAULT 'switch' for existing rows:
--   Legacy exercises were all state_switch-type operations.
--   New plan builder writes the correct value for every new step it creates.

ALTER TABLE ai_plan_steps
    ADD COLUMN IF NOT EXISTS mechanic VARCHAR(10)
        NOT NULL
        DEFAULT 'switch'
        CHECK (mechanic IN ('switch', 'unload'));


-- ── user_profiles ─────────────────────────────────────────────────────────────

-- is_paused: set to TRUE by /pause, reset to FALSE by /resume.
-- Scheduler skips delivery when TRUE.
-- Pause is NOT an adaptation — no AdaptationHistory record, no plan rewrite.

ALTER TABLE user_profiles
    ADD COLUMN IF NOT EXISTS is_paused BOOLEAN
        NOT NULL DEFAULT FALSE;

-- pause_count: monotonically increasing counter.
-- Incremented by pause_plan(), never decremented.
-- Used for engagement analytics.

ALTER TABLE user_profiles
    ADD COLUMN IF NOT EXISTS pause_count INTEGER
        NOT NULL DEFAULT 0;

-- evening_slot_collected: set to TRUE once the EVENING HH:MM is persisted
-- for the user's first MEDIUM plan. Never reset — never asked again.

ALTER TABLE user_profiles
    ADD COLUMN IF NOT EXISTS evening_slot_collected BOOLEAN
        NOT NULL DEFAULT FALSE;


COMMIT;


-- ── Rollback (run manually if needed) ─────────────────────────────────────────
-- BEGIN;
-- ALTER TABLE ai_plan_steps  DROP COLUMN IF EXISTS mechanic;
-- ALTER TABLE user_profiles  DROP COLUMN IF EXISTS is_paused;
-- ALTER TABLE user_profiles  DROP COLUMN IF EXISTS pause_count;
-- ALTER TABLE user_profiles  DROP COLUMN IF EXISTS evening_slot_collected;
-- COMMIT;
