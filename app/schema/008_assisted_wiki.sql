-- Evidence links for model-assisted Wiki candidates.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS knowledge_candidate_evidence (
    candidate_id INTEGER NOT NULL REFERENCES knowledge_candidates(id) ON DELETE CASCADE,
    evidence_id INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY(candidate_id, evidence_id)
);
CREATE INDEX IF NOT EXISTS idx_candidate_evidence_evidence
    ON knowledge_candidate_evidence(evidence_id);
