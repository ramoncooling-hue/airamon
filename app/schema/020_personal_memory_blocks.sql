-- Personal memory blocks are the durable, user-readable layers around the
-- curated knowledge graph. They are deliberately smaller than a full agent
-- memory service and remain portable with the local SQLite database.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS memory_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_key TEXT NOT NULL DEFAULT 'studypal',
    layer TEXT NOT NULL CHECK(layer IN ('identity','preference','rule','episodic','semantic','working')),
    title TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('draft','active','archived','superseded')),
    source_type TEXT NOT NULL DEFAULT 'manual',
    source_id TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.8 CHECK(confidence >= 0 AND confidence <= 1),
    confirmed INTEGER NOT NULL DEFAULT 1 CHECK(confirmed IN (0,1)),
    version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_memory_blocks_scope
    ON memory_blocks(workspace_key, status, layer, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_blocks_source
    ON memory_blocks(source_type, source_id);

CREATE TABLE IF NOT EXISTS memory_block_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_block_id INTEGER NOT NULL REFERENCES memory_blocks(id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK(version > 0),
    layer TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'manual',
    source_id TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.8,
    confirmed INTEGER NOT NULL DEFAULT 1,
    origin TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(memory_block_id, version)
);

CREATE INDEX IF NOT EXISTS idx_memory_block_revisions_block
    ON memory_block_revisions(memory_block_id, version DESC);
