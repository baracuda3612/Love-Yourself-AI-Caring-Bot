-- Add adaptation history table for adaptation safety layer.

BEGIN;

CREATE TABLE IF NOT EXISTS adaptation_history (
    id SERIAL PRIMARY KEY,
    plan_id INTEGER NOT NULL REFERENCES ai_plans(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    intent VARCHAR(60) NOT NULL,
    params JSONB NULL,
    category VARCHAR(40) NOT NULL,
    snapshot_before JSONB NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_rolled_back BOOLEAN NOT NULL DEFAULT FALSE,
    rolled_back_at TIMESTAMPTZ NULL
);

CREATE INDEX IF NOT EXISTS ix_adaptation_history_plan_id
    ON adaptation_history(plan_id);

CREATE INDEX IF NOT EXISTS ix_adaptation_history_user_id
    ON adaptation_history(user_id);

COMMIT;
