-- Canceled by adaptation: семантично відрізняється від skipped
ALTER TABLE ai_plan_steps
ADD COLUMN canceled_by_adaptation BOOLEAN NOT NULL DEFAULT FALSE;

-- Slot type: CORE | SUPPORT | REST — для майбутнього rebalancing (поки заповнюється DEFAULT)
ALTER TABLE ai_plan_steps
ADD COLUMN slot_type VARCHAR(20) NOT NULL DEFAULT 'CORE';

-- Поточний контракт слотів на плані
ALTER TABLE ai_plans
ADD COLUMN preferred_time_slots JSONB NOT NULL DEFAULT '[]';
