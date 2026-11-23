-- Structured notices imported from course groups or official school channels.
-- Notices remain separate from calendar events so the original message can be
-- searched and shown without pretending it is a scheduled item.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS notices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id TEXT NOT NULL DEFAULT '',
    course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    course_name TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    date TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'local',
    url TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(record_id)
);

CREATE INDEX IF NOT EXISTS idx_notices_date ON notices(date);
CREATE INDEX IF NOT EXISTS idx_notices_course ON notices(course_name);
