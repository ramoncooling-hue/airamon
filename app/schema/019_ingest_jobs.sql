CREATE TABLE IF NOT EXISTS ingest_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    workspace_key TEXT NOT NULL,
    path TEXT NOT NULL,
    folder TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    source_id INTEGER REFERENCES source_files(id) ON DELETE SET NULL,
    ingest_run_id INTEGER REFERENCES ingest_runs(id) ON DELETE SET NULL,
    source_kind TEXT NOT NULL DEFAULT 'unclassified',
    source_role TEXT NOT NULL DEFAULT 'material',
    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN
        ('queued','running','waiting_review','succeeded','failed','cancelled','interrupted')),
    stage TEXT NOT NULL DEFAULT 'queued',
    pages_done INTEGER NOT NULL DEFAULT 0,
    pages_total INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    retry_count INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(course_id, sha256)
);
CREATE INDEX IF NOT EXISTS idx_ingest_jobs_status ON ingest_jobs(status,id);
