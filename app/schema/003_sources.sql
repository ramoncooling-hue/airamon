CREATE TABLE IF NOT EXISTS source_files (
    id INTEGER PRIMARY KEY,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT NOT NULL DEFAULT '',
    indexed_at TEXT NOT NULL DEFAULT '',
    UNIQUE(course_id, path)
);

CREATE TABLE IF NOT EXISTS source_chunks (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    content TEXT NOT NULL,
    UNIQUE(source_id, position)
);

CREATE TABLE IF NOT EXISTS graph_candidates (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    UNIQUE(source_id, label)
);

CREATE INDEX IF NOT EXISTS idx_sources_course ON source_files(course_id);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON source_chunks(source_id);
