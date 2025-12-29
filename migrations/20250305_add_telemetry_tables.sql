CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS content_library (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content_version INTEGER NOT NULL DEFAULT 1,
    internal_name TEXT NOT NULL,
    category TEXT NOT NULL,
    difficulty INTEGER NOT NULL,
    energy_cost TEXT NOT NULL,
    logic_tags JSONB,
    content_payload JSONB,
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS plan_instances (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id INTEGER NOT NULL REFERENCES users(id),
    blueprint_id TEXT,
    initial_parameters JSONB,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_plan_instances_user_id ON plan_instances(user_id);

CREATE TABLE IF NOT EXISTS plan_execution_windows (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instance_id UUID NOT NULL REFERENCES plan_instances(id),
    engagement_status TEXT NOT NULL,
    start_date TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    end_date TIMESTAMPTZ,
    current_load_mode TEXT DEFAULT 'LITE',
    adaptation_requests_count INTEGER DEFAULT 0,
    batch_completion_count INTEGER DEFAULT 0,
    hidden_compensation_score REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_plan_execution_windows_instance_id ON plan_execution_windows(instance_id);

CREATE TABLE IF NOT EXISTS user_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    user_id INTEGER NOT NULL REFERENCES users(id),
    plan_execution_id UUID NOT NULL REFERENCES plan_execution_windows(id),
    step_id UUID REFERENCES content_library(id),
    time_of_day_bucket TEXT NOT NULL,
    context JSONB
);
CREATE INDEX IF NOT EXISTS idx_user_events_user_id ON user_events(user_id);
CREATE INDEX IF NOT EXISTS idx_user_events_plan_execution_id ON user_events(plan_execution_id);
CREATE INDEX IF NOT EXISTS idx_user_events_step_id ON user_events(step_id);
CREATE INDEX IF NOT EXISTS idx_user_events_context_gin ON user_events USING GIN (context);

CREATE TABLE IF NOT EXISTS task_stats (
    user_id INTEGER NOT NULL REFERENCES users(id),
    step_id UUID NOT NULL REFERENCES content_library(id),
    attempts_total INTEGER DEFAULT 0,
    completed_total INTEGER DEFAULT 0,
    skipped_total INTEGER DEFAULT 0,
    avg_reaction_sec REAL DEFAULT 0,
    completed_edge_of_day INTEGER DEFAULT 0,
    last_failure_reason TEXT,
    history_ref BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (user_id, step_id)
);

CREATE TABLE IF NOT EXISTS failure_signals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id INTEGER NOT NULL REFERENCES users(id),
    plan_execution_id UUID NOT NULL REFERENCES plan_execution_windows(id),
    step_id UUID NOT NULL REFERENCES content_library(id),
    trigger_event TEXT NOT NULL,
    failure_context_tag TEXT,
    detected_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_failure_signals_user_id ON failure_signals(user_id);
CREATE INDEX IF NOT EXISTS idx_failure_signals_plan_execution_id ON failure_signals(plan_execution_id);
