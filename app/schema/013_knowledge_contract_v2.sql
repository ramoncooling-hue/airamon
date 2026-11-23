-- Structured metadata that turns the knowledge contract into durable runtime data.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS knowledge_node_meta (
    kp_id INTEGER PRIMARY KEY REFERENCES kp(id) ON DELETE CASCADE,
    schema_version TEXT NOT NULL DEFAULT 'cyberpal.knowledge.v1',
    domain TEXT NOT NULL DEFAULT 'mba',
    attributes_json TEXT NOT NULL DEFAULT '{}',
    source_refs_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'confirmed',
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS kp_evidence (
    kp_id INTEGER NOT NULL REFERENCES kp(id) ON DELETE CASCADE,
    evidence_id INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'supports',
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY(kp_id, evidence_id)
);
CREATE INDEX IF NOT EXISTS idx_kp_evidence_evidence ON kp_evidence(evidence_id);

CREATE TABLE IF NOT EXISTS knowledge_relation_meta (
    edge_id INTEGER PRIMARY KEY REFERENCES kp_edges(id) ON DELETE CASCADE,
    schema_version TEXT NOT NULL DEFAULT 'cyberpal.knowledge.v1',
    evidence_id INTEGER REFERENCES evidence(id) ON DELETE SET NULL,
    reason TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.5,
    status TEXT NOT NULL DEFAULT 'confirmed',
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);
