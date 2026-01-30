BEGIN;

CREATE TABLE IF NOT EXISTS plan_drafts (
    id UUID PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    status VARCHAR(20) NOT NULL DEFAULT 'DRAFT',
    duration VARCHAR(20) NOT NULL,
    focus VARCHAR(20) NOT NULL,
    load VARCHAR(20) NOT NULL,
    draft_data JSONB NOT NULL,
    total_days INTEGER NOT NULL,
    total_steps INTEGER NOT NULL,
    is_valid BOOLEAN NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_plan_drafts_user_id ON plan_drafts (user_id);

CREATE TABLE IF NOT EXISTS plan_draft_steps (
    id UUID PRIMARY KEY,
    draft_id UUID NOT NULL REFERENCES plan_drafts(id) ON DELETE CASCADE,
    day_number INTEGER NOT NULL,
    exercise_id VARCHAR(50) NOT NULL,
    slot_type VARCHAR(20) NOT NULL,
    time_slot VARCHAR(20) NOT NULL,
    category VARCHAR(30) NOT NULL,
    difficulty INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_plan_draft_steps_draft_id ON plan_draft_steps (draft_id);

COMMIT;
