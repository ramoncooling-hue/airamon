-- @idempotent-alter
-- T2.1 / O-DATA-4: consolidate the legacy inline schema into a numbered migration.
--
-- Until 0.5.7 these ALTER/backfill statements lived in db._legacy_upgrade and
-- ran after every numbered schema file, so a production database could not be
-- rebuilt from app/schema alone. They now live here; db._legacy_upgrade keeps
-- only the immutable 018z_legacy_inline_v1 compatibility identity. The marker
-- lets databases that already carry a column skip the matching ALTER (SQLite
-- has no ALTER TABLE ... ADD COLUMN IF NOT EXISTS).
ALTER TABLE courses ADD COLUMN workspace_id INTEGER REFERENCES workspaces(id) ON DELETE SET NULL;
ALTER TABLE events ADD COLUMN workspace_id INTEGER REFERENCES workspaces(id) ON DELETE SET NULL;
ALTER TABLE conversations ADD COLUMN workspace_id INTEGER REFERENCES workspaces(id) ON DELETE SET NULL;
ALTER TABLE workspace_items ADD COLUMN path TEXT NOT NULL DEFAULT '';
ALTER TABLE kp ADD COLUMN chapter_key TEXT NOT NULL DEFAULT '';
ALTER TABLE kp ADD COLUMN section_key TEXT NOT NULL DEFAULT '';
ALTER TABLE chapter_catalog ADD COLUMN catalog_version TEXT NOT NULL DEFAULT '1';
ALTER TABLE source_files ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'unclassified';
ALTER TABLE source_files ADD COLUMN source_role TEXT NOT NULL DEFAULT 'material';
ALTER TABLE source_files ADD COLUMN catalog_status TEXT NOT NULL DEFAULT 'none';
ALTER TABLE source_files ADD COLUMN ingest_run_id INTEGER REFERENCES ingest_runs(id) ON DELETE SET NULL;
ALTER TABLE events ADD COLUMN source TEXT NOT NULL DEFAULT 'local';
ALTER TABLE events ADD COLUMN source_uid TEXT NOT NULL DEFAULT '';
ALTER TABLE events ADD COLUMN source_updated_at TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_kp_chapter_key ON kp(course_id, chapter_key);
CREATE INDEX IF NOT EXISTS idx_sources_kind
    ON source_files(course_id, source_kind, catalog_status);
CREATE INDEX IF NOT EXISTS idx_courses_workspace ON courses(workspace_id);
CREATE INDEX IF NOT EXISTS idx_events_workspace ON events(workspace_id);
CREATE INDEX IF NOT EXISTS idx_conversations_workspace ON conversations(workspace_id);

-- Older runtimes created chapter_catalog directly from a textbook and predate
-- source_kind. Preserve that known source as a promotable candidate instead of
-- losing navigation; the WHERE makes this replay-safe.
UPDATE source_files
   SET source_kind='course_catalog', source_role='catalog', catalog_status='candidate'
 WHERE source_kind='unclassified'
   AND id IN (SELECT DISTINCT source_id FROM chapter_catalog WHERE source_id IS NOT NULL);

-- Retire the old same-source cartesian candidate generator. It linked every
-- pair of concepts sharing a source, not a semantic relation.
UPDATE relation_candidates
   SET status='rejected', reviewed_at=datetime('now','localtime')
 WHERE status='pending'
   AND reason='同一份材料中的已确认节点，待人工审核';

-- Capability markers: some embedded SQLite builds omit FTS5 or its trigram
-- tokenizer. The migration engine records what this database was built with
-- (and whether 011 was skipped) so degraded installs stay diagnosable.
CREATE TABLE IF NOT EXISTS schema_capabilities (
    name       TEXT PRIMARY KEY,
    detail     TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
