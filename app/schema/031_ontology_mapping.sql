-- Mapping content is append-only; review changes only its lifecycle metadata.
CREATE TABLE IF NOT EXISTS ontology_mapping_versions (
    id INTEGER PRIMARY KEY,
    workspace_key TEXT NOT NULL,
    ontology_version TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    profile_hash TEXT NOT NULL,
    source_scope_hash TEXT NOT NULL,
    mapping_json TEXT NOT NULL,
    bindings_json TEXT NOT NULL,
    mapping_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft','pending_review','approved','rejected','superseded')),
    base_version_id INTEGER REFERENCES ontology_mapping_versions(id),
    created_by TEXT NOT NULL,
    approved_by TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    approved_at TEXT,
    rejected_at TEXT,
    note TEXT NOT NULL DEFAULT '',
    UNIQUE (workspace_key, mapping_hash),
    CHECK (
        (status IN ('draft','pending_review') AND approved_by IS NULL
            AND approved_at IS NULL AND rejected_at IS NULL)
        OR (status IN ('approved','superseded') AND approved_by IS NOT NULL
            AND length(trim(approved_by)) > 0 AND approved_at IS NOT NULL AND rejected_at IS NULL)
        OR (status='rejected' AND approved_by IS NULL
            AND approved_at IS NULL AND rejected_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_ontology_mappings_workspace_status
    ON ontology_mapping_versions(workspace_key, status, id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ontology_mappings_active_scope
    ON ontology_mapping_versions(workspace_key, profile_id, source_scope_hash)
    WHERE status='approved';

CREATE TRIGGER IF NOT EXISTS ontology_mappings_insert_draft
BEFORE INSERT ON ontology_mapping_versions
WHEN NEW.status != 'draft'
BEGIN
    SELECT RAISE(ABORT, 'Mapping versions must start as drafts');
END;

-- REPLACE may skip delete triggers when recursive_triggers is disabled.
-- Reject conflicts before SQLite can delete and replace immutable history.
CREATE TRIGGER IF NOT EXISTS ontology_mappings_no_replace
BEFORE INSERT ON ontology_mapping_versions
WHEN EXISTS (SELECT 1 FROM ontology_mapping_versions WHERE id=NEW.id)
    OR EXISTS (SELECT 1 FROM ontology_mapping_versions
               WHERE workspace_key=NEW.workspace_key AND mapping_hash=NEW.mapping_hash)
BEGIN
    SELECT RAISE(ABORT, 'Mapping version already exists');
END;

CREATE TRIGGER IF NOT EXISTS ontology_mappings_base_scope
BEFORE INSERT ON ontology_mapping_versions
WHEN NEW.base_version_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM ontology_mapping_versions b
    WHERE b.id=NEW.base_version_id AND b.workspace_key=NEW.workspace_key
        AND b.profile_id=NEW.profile_id AND b.source_scope_hash=NEW.source_scope_hash
        AND b.status IN ('approved','superseded')
)
BEGIN
    SELECT RAISE(ABORT, 'Mapping base must be an approved version in the same scope');
END;

CREATE TRIGGER IF NOT EXISTS ontology_mappings_immutable
BEFORE UPDATE ON ontology_mapping_versions
WHEN NEW.id IS NOT OLD.id OR NEW.workspace_key IS NOT OLD.workspace_key
    OR NEW.ontology_version IS NOT OLD.ontology_version
    OR NEW.profile_id IS NOT OLD.profile_id OR NEW.profile_version IS NOT OLD.profile_version
    OR NEW.profile_hash IS NOT OLD.profile_hash OR NEW.source_scope_hash IS NOT OLD.source_scope_hash
    OR NEW.mapping_json IS NOT OLD.mapping_json OR NEW.bindings_json IS NOT OLD.bindings_json
    OR NEW.mapping_hash IS NOT OLD.mapping_hash OR NEW.base_version_id IS NOT OLD.base_version_id
    OR NEW.created_by IS NOT OLD.created_by OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'Mapping content and provenance are immutable');
END;

CREATE TRIGGER IF NOT EXISTS ontology_mappings_transition
BEFORE UPDATE ON ontology_mapping_versions
WHEN NOT (
    (OLD.status='draft' AND NEW.status='pending_review')
    OR (OLD.status='pending_review' AND NEW.status IN ('approved','rejected'))
    OR (OLD.status='approved' AND NEW.status='superseded')
)
BEGIN
    SELECT RAISE(ABORT, 'Invalid mapping status transition');
END;

-- UPDATE OR REPLACE can also delete the row owning the partial unique key.
-- Approval must first supersede the previous version in the same transaction.
CREATE TRIGGER IF NOT EXISTS ontology_mappings_no_approval_replace
BEFORE UPDATE ON ontology_mapping_versions
WHEN NEW.status='approved' AND EXISTS (
    SELECT 1 FROM ontology_mapping_versions
    WHERE id != OLD.id AND status='approved'
        AND workspace_key=NEW.workspace_key AND profile_id=NEW.profile_id
        AND source_scope_hash=NEW.source_scope_hash
)
BEGIN
    SELECT RAISE(ABORT, 'Approved mapping already exists in source scope');
END;

CREATE TRIGGER IF NOT EXISTS ontology_mappings_review_immutable
BEFORE UPDATE ON ontology_mapping_versions
WHEN (OLD.status IN ('draft','approved') AND (
        NEW.approved_by IS NOT OLD.approved_by OR NEW.approved_at IS NOT OLD.approved_at
        OR NEW.rejected_at IS NOT OLD.rejected_at OR NEW.note IS NOT OLD.note))
    OR (NEW.status='approved' AND NEW.rejected_at IS NOT OLD.rejected_at)
    OR (NEW.status='rejected' AND (
        NEW.approved_by IS NOT OLD.approved_by OR NEW.approved_at IS NOT OLD.approved_at))
BEGIN
    SELECT RAISE(ABORT, 'Mapping review history is immutable');
END;

CREATE TRIGGER IF NOT EXISTS ontology_mappings_no_delete
BEFORE DELETE ON ontology_mapping_versions
BEGIN
    SELECT RAISE(ABORT, 'Mapping history cannot be deleted');
END;
