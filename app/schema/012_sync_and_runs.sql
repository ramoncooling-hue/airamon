-- Cross-device-friendly local exchange metadata and Hub execution records.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS event_sync_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    source_uid TEXT NOT NULL,
    local_json TEXT NOT NULL,
    incoming_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted_local','accepted_incoming')),
    created_at TEXT DEFAULT (datetime('now','localtime')),
    resolved_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_event_conflicts_status ON event_sync_conflicts(status, id DESC);

CREATE TABLE IF NOT EXISTS hub_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_key TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running' CHECK(status IN ('running','success','error')),
    duration_ms INTEGER NOT NULL DEFAULT 0,
    tool_summary TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_hub_runs_started ON hub_runs(started_at DESC);
