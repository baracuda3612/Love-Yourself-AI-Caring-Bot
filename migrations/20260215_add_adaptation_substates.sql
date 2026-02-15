ALTER TABLE users
DROP CONSTRAINT IF EXISTS ck_users_current_state;

ALTER TABLE users
ADD CONSTRAINT ck_users_current_state
CHECK (
    current_state IN (
        'IDLE_NEW',
        'IDLE_ONBOARDED',
        'IDLE_PLAN_ABORTED',
        'IDLE_FINISHED',
        'IDLE_DROPPED',
        'PLAN_FLOW:DATA_COLLECTION',
        'PLAN_FLOW:CONFIRMATION_PENDING',
        'PLAN_FLOW:FINALIZATION',
        'ACTIVE',
        'ACTIVE_CONFIRMATION',
        'ACTIVE_PAUSED',
        'ACTIVE_PAUSED_CONFIRMATION',
        'ADAPTATION_SELECTION',
        'ADAPTATION_PARAMS',
        'ADAPTATION_CONFIRMATION'
    )
    OR current_state LIKE 'ONBOARDING:%'
);
