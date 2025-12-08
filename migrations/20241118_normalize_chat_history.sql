BEGIN;
DROP TABLE IF EXISTS chat_history_new;
CREATE TABLE chat_history_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    role TEXT NOT NULL CHECK (role in ('user','assistant')),
    text TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO chat_history_new (id, user_id, role, text, created_at)
SELECT
    id,
    user_id,
    CASE
        WHEN role IN ('user','assistant') THEN role
        ELSE 'assistant'
    END AS role,
    content AS text,
    COALESCE(created_at, CURRENT_TIMESTAMP) AS created_at
FROM chat_history;
DROP TABLE chat_history;
ALTER TABLE chat_history_new RENAME TO chat_history;
CREATE INDEX IF NOT EXISTS idx_chat_history_user_id ON chat_history(user_id);
COMMIT;
