-- T3.3 Work Days + Task Lifecycle Overhaul
-- Adds: user_profiles.active_days, ai_plan_steps.step_status, ai_plan_steps.expires_at
-- Backfills step_status from legacy boolean columns.

BEGIN;

-- ── user_profiles ────────────────────────────────────────────────────────────
-- active_days: JSONB list of weekday codes the user wants tasks delivered on.
-- Default = Mon–Fri. User can override via onboarding or settings.

ALTER TABLE user_profiles
    ADD COLUMN IF NOT EXISTS active_days JSONB
        NOT NULL DEFAULT '["MON","TUE","WED","THU","FRI"]'::jsonb;


-- ── ai_plan_steps ─────────────────────────────────────────────────────────────
-- step_status: canonical state machine column replacing boolean flags.
-- Values: pending | delivered | completed | skipped | expired

ALTER TABLE ai_plan_steps
    ADD COLUMN IF NOT EXISTS step_status VARCHAR(20)
        NOT NULL DEFAULT 'pending';

-- expires_at: 23:59:59 on the scheduled calendar day in the user's timezone
-- (stored as TIMESTAMPTZ). NULL for legacy rows without an expiry.

ALTER TABLE ai_plan_steps
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ NULL;


-- ── Backfill step_status ──────────────────────────────────────────────────────
-- Priority: completed > skipped > canceled_by_adaptation (→ canceled) > pending.
-- IMPORTANT: canceled_by_adaptation maps to 'canceled', NOT 'expired'.
--   expired  = user had a window and did not react  (churn signal)
--   canceled = system removed the step via adaptation layer (excluded from metrics)

UPDATE ai_plan_steps
SET step_status =
    CASE
        WHEN is_completed           = TRUE THEN 'completed'
        WHEN skipped                = TRUE THEN 'skipped'
        WHEN canceled_by_adaptation = TRUE THEN 'canceled'
        ELSE 'pending'
    END
WHERE step_status = 'pending';  -- idempotent: only touches rows not yet set


-- ── Backfill expires_at for existing steps ────────────────────────────────────
-- Steps created before this migration have no expires_at.
-- Fallback: end of the scheduled_for day in UTC (23:59:59).
-- This gives the expiry job something to work with for legacy active plans.

UPDATE ai_plan_steps
SET expires_at = date_trunc('day', scheduled_for AT TIME ZONE 'UTC')
                 + INTERVAL '1 day' - INTERVAL '1 second'
WHERE expires_at IS NULL
  AND scheduled_for IS NOT NULL
  AND step_status IN ('pending', 'delivered');


-- ── Index for common filter: pending/delivered steps per plan ─────────────────

CREATE INDEX IF NOT EXISTS ix_ai_plan_steps_step_status
    ON ai_plan_steps(step_status);

COMMIT;


-- ── Rollback (run manually if needed) ────────────────────────────────────────
-- Note: expires_at backfill is not reversible without original data.
-- BEGIN;
-- ALTER TABLE ai_plan_steps  DROP COLUMN IF EXISTS expires_at;
-- ALTER TABLE ai_plan_steps  DROP COLUMN IF EXISTS step_status;
-- ALTER TABLE user_profiles  DROP COLUMN IF EXISTS active_days;
-- DROP INDEX IF EXISTS ix_ai_plan_steps_step_status;
-- COMMIT;
