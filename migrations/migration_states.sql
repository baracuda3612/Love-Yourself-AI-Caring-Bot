BEGIN;

-- 1. Remove old constraint
ALTER TABLE users
DROP CONSTRAINT IF EXISTS ck_users_current_state;

-- 2. Add updated constraint synced with states.py
ALTER TABLE users
ADD CONSTRAINT ck_users_current_state CHECK (
    current_state IN (
        -- IDLE
        'IDLE_NEW',
        'IDLE_ONBOARDED',
        'IDLE_PLAN_ABORTED',
        'IDLE_FINISHED',
        'IDLE_DROPPED',

        -- ACTIVE
        'ACTIVE',
        'ACTIVE_CONFIRMATION',
        'ACTIVE_PAUSED',
        'ACTIVE_PAUSED_CONFIRMATION',

        -- ADAPTATION
        'ADAPTATION_FLOW'
    )
    OR current_state LIKE 'PLAN_FLOW:%'
);

COMMIT;
