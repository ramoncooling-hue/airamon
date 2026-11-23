-- Version history for locally edited Agent/Skill prompts.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS hub_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK(kind IN ('skill', 'agent')),
    key TEXT NOT NULL,
    version TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_hub_revisions_key
    ON hub_revisions(kind, key, id DESC);
