-- @idempotent-alter
-- T1.6 / O-DATA-1: rescanning keeps prior extraction as stale history.
--
-- A rescan used to delete source_chunks/evidence/knowledge_candidates, which
-- cascaded into candidate_reviews and kp_evidence. Instead rows are retained
-- and marked stale; new extraction rows point at a new source_snapshots row.
-- Reviewed candidates (confirmed/duplicate/merged/rejected) are never removed,
-- so historical kp_evidence stays traceable.
CREATE TABLE IF NOT EXISTS source_snapshots (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
    sha256 TEXT NOT NULL,
    extracted_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(source_id, sha256)
);
CREATE INDEX IF NOT EXISTS idx_source_snapshots_source
    ON source_snapshots(source_id, id);

ALTER TABLE evidence ADD COLUMN snapshot_id INTEGER REFERENCES source_snapshots(id);
ALTER TABLE evidence ADD COLUMN stale INTEGER NOT NULL DEFAULT 0;
ALTER TABLE source_chunks ADD COLUMN snapshot_id INTEGER;
ALTER TABLE source_chunks ADD COLUMN stale INTEGER NOT NULL DEFAULT 0;
ALTER TABLE knowledge_candidates ADD COLUMN snapshot_id INTEGER;
ALTER TABLE knowledge_candidates ADD COLUMN superseded_by INTEGER REFERENCES knowledge_candidates(id);
ALTER TABLE candidate_reviews ADD COLUMN superseded_at TEXT;

CREATE INDEX IF NOT EXISTS idx_evidence_source_stale ON evidence(source_id, stale);
CREATE INDEX IF NOT EXISTS idx_chunks_source_stale ON source_chunks(source_id, stale);
CREATE INDEX IF NOT EXISTS idx_knowledge_candidates_snapshot
    ON knowledge_candidates(source_id, snapshot_id);
