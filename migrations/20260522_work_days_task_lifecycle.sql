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

-- tg_message_id: Telegram message ID of the delivered task notification.
-- Used by expire_overdue_steps to remove inline keyboard buttons on expiry.

ALTER TABLE ai_plan_steps
    ADD COLUMN IF NOT EXISTS tg_message_id INTEGER NULL;


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
-- Backfill rule must match runtime lifecycle exactly:
-- scheduled_for -> user's local timezone -> same local day 23:59:59 -> back to UTC.
-- Using UTC truncation here would expire some tasks hours too early / too late.

UPDATE ai_plan_steps AS step
SET expires_at =
    (
        date_trunc(
            'day',
            step.scheduled_for AT TIME ZONE COALESCE(NULLIF("user".timezone, ''), 'Europe/Kyiv')
        )
        + INTERVAL '1 day'
        - INTERVAL '1 second'
    ) AT TIME ZONE COALESCE(NULLIF("user".timezone, ''), 'Europe/Kyiv')
FROM ai_plan_days AS day
JOIN ai_plans AS plan ON plan.id = day.plan_id
JOIN users AS "user" ON "user".id = plan.user_id
WHERE step.day_id = day.id
  AND step.expires_at IS NULL
  AND step.scheduled_for IS NOT NULL
  AND step.step_status IN ('pending', 'delivered');


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
