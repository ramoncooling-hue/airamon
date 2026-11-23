CREATE TABLE IF NOT EXISTS knowledge_candidates (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
    schema_version TEXT NOT NULL,
    name TEXT NOT NULL,
    kp_type TEXT NOT NULL DEFAULT 'material',
    chapter TEXT NOT NULL DEFAULT '',
    gist TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    trap TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.55,
    status TEXT NOT NULL DEFAULT 'pending',
    UNIQUE(source_id, name)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_candidates_source ON knowledge_candidates(source_id);
