-- Explicit, source-scoped extraction previews. Never publishes knowledge rows.
CREATE TABLE IF NOT EXISTS mapping_extraction_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_key TEXT NOT NULL,
    mapping_version_id INTEGER NOT NULL REFERENCES ontology_mapping_versions(id),
    mapping_hash TEXT NOT NULL,
    source_ids_json TEXT NOT NULL,
    source_state_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ready'
        CHECK (status IN ('ready','running','partial','completed','cancelled','conflict')),
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (workspace_key,mapping_version_id,mapping_hash,source_ids_json)
);

CREATE TABLE IF NOT EXISTS mapping_extraction_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES mapping_extraction_runs(id),
    evidence_id INTEGER NOT NULL,
    source_id INTEGER NOT NULL,
    snapshot_id INTEGER NOT NULL,
    start INTEGER NOT NULL CHECK (start >= 0),
    end INTEGER NOT NULL CHECK (end > start AND end-start <= 4000),
    evidence_text TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    segment_sha256 TEXT NOT NULL,
    binding_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','running','succeeded','failed','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    claim_token TEXT,
    claimed_at REAL,
    error TEXT NOT NULL DEFAULT '',
    output_json TEXT,
    UNIQUE (run_id,evidence_id,start,end),
    CHECK ((status='running' AND claim_token IS NOT NULL AND claimed_at IS NOT NULL)
        OR (status!='running' AND claim_token IS NULL AND claimed_at IS NULL)),
    CHECK ((status='succeeded' AND output_json IS NOT NULL)
        OR (status!='succeeded' AND output_json IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_mapping_extraction_workspace
    ON mapping_extraction_runs(workspace_key,mapping_version_id,id DESC);
CREATE INDEX IF NOT EXISTS idx_mapping_extraction_segments
    ON mapping_extraction_segments(run_id,status,id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mapping_extraction_live_claim
    ON mapping_extraction_segments(run_id) WHERE status='running';

CREATE TRIGGER IF NOT EXISTS mapping_extraction_runs_immutable
BEFORE UPDATE ON mapping_extraction_runs
WHEN NEW.id IS NOT OLD.id OR NEW.workspace_key IS NOT OLD.workspace_key
    OR NEW.mapping_version_id IS NOT OLD.mapping_version_id OR NEW.mapping_hash IS NOT OLD.mapping_hash
    OR NEW.source_ids_json IS NOT OLD.source_ids_json OR NEW.source_state_json IS NOT OLD.source_state_json
    OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT,'Extraction provenance is immutable');
END;

CREATE TRIGGER IF NOT EXISTS mapping_extraction_segments_immutable
BEFORE UPDATE ON mapping_extraction_segments
WHEN NEW.id IS NOT OLD.id OR NEW.run_id IS NOT OLD.run_id
    OR NEW.evidence_id IS NOT OLD.evidence_id OR NEW.source_id IS NOT OLD.source_id
    OR NEW.snapshot_id IS NOT OLD.snapshot_id OR NEW.start IS NOT OLD.start OR NEW.end IS NOT OLD.end
    OR NEW.evidence_text IS NOT OLD.evidence_text OR NEW.source_sha256 IS NOT OLD.source_sha256
    OR NEW.evidence_sha256 IS NOT OLD.evidence_sha256 OR NEW.segment_sha256 IS NOT OLD.segment_sha256
    OR NEW.binding_hash IS NOT OLD.binding_hash OR OLD.status='succeeded'
BEGIN
    SELECT RAISE(ABORT,'Extraction provenance and successful output are immutable');
END;

CREATE TRIGGER IF NOT EXISTS mapping_extraction_runs_no_replace
BEFORE INSERT ON mapping_extraction_runs
WHEN EXISTS (SELECT 1 FROM mapping_extraction_runs WHERE id=NEW.id
    OR (workspace_key=NEW.workspace_key AND mapping_version_id=NEW.mapping_version_id
        AND mapping_hash=NEW.mapping_hash AND source_ids_json=NEW.source_ids_json))
BEGIN
    SELECT RAISE(ABORT,'Extraction run already exists');
END;

CREATE TRIGGER IF NOT EXISTS mapping_extraction_segments_no_replace
BEFORE INSERT ON mapping_extraction_segments
WHEN EXISTS (SELECT 1 FROM mapping_extraction_segments WHERE id=NEW.id
    OR (run_id=NEW.run_id AND evidence_id=NEW.evidence_id AND start=NEW.start AND end=NEW.end))
BEGIN
    SELECT RAISE(ABORT,'Extraction segment already exists');
END;

CREATE TRIGGER IF NOT EXISTS mapping_extraction_runs_no_delete
BEFORE DELETE ON mapping_extraction_runs
BEGIN
    SELECT RAISE(ABORT,'Extraction history is immutable');
END;

CREATE TRIGGER IF NOT EXISTS mapping_extraction_segments_no_delete
BEFORE DELETE ON mapping_extraction_segments
BEGIN
    SELECT RAISE(ABORT,'Extraction history is immutable');
END;
