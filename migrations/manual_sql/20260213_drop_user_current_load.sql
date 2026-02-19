-- Safety check
SELECT current_load
FROM users
LIMIT 1;

-- If column exists, drop it:
ALTER TABLE users
DROP COLUMN IF EXISTS current_load;
