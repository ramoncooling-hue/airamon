-- Local raw/text snapshots used for source diff and stale-file recovery.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS source_version_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_version_id INTEGER NOT NULL REFERENCES source_versions(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
    sha256 TEXT NOT NULL,
    original_path TEXT NOT NULL,
    raw_path TEXT NOT NULL,
    text_path TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(source_version_id)
);
CREATE INDEX IF NOT EXISTS idx_source_version_artifacts_source
    ON source_version_artifacts(source_id, sha256);
