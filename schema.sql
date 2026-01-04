CREATE TABLE IF NOT EXISTS users (
  user_id INTEGER PRIMARY KEY,
  tz TEXT,
  last_seen TEXT
);

CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER,
  user_id INTEGER,
  text TEXT,
  run_at TEXT,                 -- ISO UTC
  recurrence TEXT,             -- NULL, 'daily', 'weekly', 'monthly'
  recurrence_detail TEXT,
  active INTEGER DEFAULT 1,

  -- follow-up state
  last_sent_at TEXT,
  last_message_id INTEGER,
  pending_followup INTEGER DEFAULT 0
);

-- Optional indexes (nice to have)
CREATE INDEX IF NOT EXISTS idx_reminders_user ON reminders(user_id);
CREATE INDEX IF NOT EXISTS idx_reminders_active_runat ON reminders(active, run_at);
