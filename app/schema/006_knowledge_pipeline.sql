-- CyberPal knowledge pipeline and lightweight local Hub.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
    chunk_id INTEGER REFERENCES source_chunks(id) ON DELETE SET NULL,
    locator_type TEXT NOT NULL DEFAULT 'chunk',
    locator_start INTEGER,
    locator_end INTEGER,
    content TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(source_id, locator_type, locator_start, locator_end, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_evidence_source ON evidence(source_id);

CREATE TABLE IF NOT EXISTS source_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
    sha256 TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'indexed',
    captured_at TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(source_id, sha256)
);
CREATE INDEX IF NOT EXISTS idx_source_versions_source ON source_versions(source_id);

CREATE TABLE IF NOT EXISTS knowledge_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    schema_version TEXT NOT NULL DEFAULT 'cyberpal.note.v1',
    status TEXT NOT NULL DEFAULT 'draft',
    updated_at TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(source_id, source_sha256)
);

CREATE TABLE IF NOT EXISTS relation_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER REFERENCES source_files(id) ON DELETE SET NULL,
    from_kp INTEGER REFERENCES kp(id) ON DELETE SET NULL,
    to_kp INTEGER REFERENCES kp(id) ON DELETE SET NULL,
    from_name TEXT NOT NULL DEFAULT '',
    to_name TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'related',
    evidence_id INTEGER REFERENCES evidence(id) ON DELETE SET NULL,
    confidence REAL NOT NULL DEFAULT 0.5,
    status TEXT NOT NULL DEFAULT 'pending',
    target_edge_id INTEGER REFERENCES kp_edges(id) ON DELETE SET NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    reviewed_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_relation_candidates_status ON relation_candidates(status);

CREATE TABLE IF NOT EXISTS candidate_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES knowledge_candidates(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    target_kp_id INTEGER REFERENCES kp(id) ON DELETE SET NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_candidate_reviews_candidate ON candidate_reviews(candidate_id);

CREATE TABLE IF NOT EXISTS kp_identities (
    kp_id INTEGER PRIMARY KEY REFERENCES kp(id) ON DELETE CASCADE,
    node_key TEXT NOT NULL UNIQUE,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS kp_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kp_id INTEGER NOT NULL REFERENCES kp(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(kp_id, alias)
);
CREATE INDEX IF NOT EXISTS idx_kp_aliases_alias ON kp_aliases(alias);

CREATE TABLE IF NOT EXISTS event_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER,
    action TEXT NOT NULL,
    old_json TEXT NOT NULL DEFAULT '{}',
    new_json TEXT NOT NULL DEFAULT '{}',
    origin TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_event_revisions_event ON event_revisions(event_id);

CREATE TABLE IF NOT EXISTS skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '1.0.0',
    enabled INTEGER NOT NULL DEFAULT 1,
    manifest_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '1.0.0',
    enabled INTEGER NOT NULL DEFAULT 1,
    manifest_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS agent_skills (
    agent_id INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    skill_id INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    priority INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(agent_id, skill_id)
);
