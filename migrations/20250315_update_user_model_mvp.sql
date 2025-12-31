-- 1. Normalize existing 'idle' users to 'IDLE_NEW'
UPDATE users
SET current_state = 'IDLE_NEW'
WHERE current_state = 'idle' OR current_state IS NULL;

-- 2. Normalize 'active' users to 'ACTIVE'
UPDATE users
SET current_state = 'ACTIVE'
WHERE current_state = 'active';

-- 3. Uppercase all ONBOARDING states (e.g. 'onboarding:start' -> 'ONBOARDING:START')
UPDATE users
SET current_state = UPPER(current_state)
WHERE current_state LIKE 'onboarding:%';

-- 4. Uppercase all PLAN_FLOW states if any exist
UPDATE users
SET current_state = UPPER(current_state)
WHERE current_state LIKE 'plan_flow:%';

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS execution_policy TEXT NOT NULL DEFAULT 'EXECUTION',
    ADD COLUMN IF NOT EXISTS current_load TEXT NOT NULL DEFAULT 'LITE',
    ADD COLUMN IF NOT EXISTS last_active_at TIMESTAMPTZ;

ALTER TABLE users
    ALTER COLUMN current_state SET DEFAULT 'IDLE_NEW';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_users_current_state'
    ) THEN
        ALTER TABLE users
            ADD CONSTRAINT ck_users_current_state
            CHECK (
                current_state IN (
                    'IDLE_NEW',
                    'IDLE_FINISHED',
                    'IDLE_DROPPED',
                    'PLAN_FLOW:DATA_COLLECTION',
                    'PLAN_FLOW:CONFIRMATION_PENDING',
                    'PLAN_FLOW:FINALIZATION',
                    'ACTIVE',
                    'ADAPTATION_FLOW'
                )
                OR current_state LIKE 'ONBOARDING:%'
            );
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_users_execution_policy'
    ) THEN
        ALTER TABLE users
            ADD CONSTRAINT ck_users_execution_policy
            CHECK (execution_policy IN ('EXECUTION', 'OBSERVATION'));
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_users_current_load'
    ) THEN
        ALTER TABLE users
            ADD CONSTRAINT ck_users_current_load
            CHECK (current_load IN ('LITE', 'MID', 'INTENSIVE'));
    END IF;
END
$$;
