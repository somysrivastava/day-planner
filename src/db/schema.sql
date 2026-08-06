-- Users of the WhatsApp bot
CREATE TABLE IF NOT EXISTS users (
  id SERIAL PRIMARY KEY,
  phone_number TEXT NOT NULL UNIQUE,
  timezone TEXT NOT NULL DEFAULT 'UTC',
  briefing_time TIME NOT NULL DEFAULT '07:30:00',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Reminders, tasks, and fixed events all live here, distinguished by `type`.
-- Type-specific fields are explicit nullable columns rather than a JSONB
-- blob, so Postgres itself enforces column types (e.g. start_time really
-- is a timestamp, not whatever shape the app happened to write).
-- Usage per type:
--   reminder:     recurrence_rule set, start_time/end_time null
--   task:         start_time/end_time set once auto-fit schedules it, null until then
--   fixed_event:  start_time/end_time set from the moment it's created
CREATE TABLE IF NOT EXISTS items (
  id SERIAL PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  type TEXT NOT NULL CHECK (type IN ('reminder', 'task', 'fixed_event')),
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'cancelled')),
  start_time TIMESTAMPTZ,
  end_time TIMESTAMPTZ,
  recurrence_rule TEXT,
  google_event_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_items_user_id ON items(user_id);
CREATE INDEX IF NOT EXISTS idx_items_type ON items(type);
