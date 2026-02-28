ALTER TABLE user_profiles
  ADD COLUMN IF NOT EXISTS coach_persona VARCHAR(20) NULL,
  ADD COLUMN IF NOT EXISTS pulse_sent_indices JSONB NULL DEFAULT '[]'::jsonb;
