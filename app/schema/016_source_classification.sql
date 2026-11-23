-- Source classification and extraction-run audit trail.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS ingest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    source_id INTEGER REFERENCES source_files(id) ON DELETE SET NULL,
    source_kind TEXT NOT NULL DEFAULT 'unclassified',
    source_role TEXT NOT NULL DEFAULT 'material',
    status TEXT NOT NULL DEFAULT 'running',
    extractor TEXT NOT NULL DEFAULT 'local',
    error TEXT NOT NULL DEFAULT '',
    artifact_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    finished_at TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_ingest_runs_course_status
    ON ingest_runs(course_id, status, id);
