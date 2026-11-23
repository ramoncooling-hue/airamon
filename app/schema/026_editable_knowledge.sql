-- @idempotent-alter
-- T2.20 / O-KB-1 (B2): editable knowledge documents become first-class entities.
--
-- A document is a Markdown body plus revision history. kp stays the summary
-- node of the graph: document bodies are written only through the documents
-- API, and kp.detail/gist/trap are never rewritten by a document edit, so a
-- rescan can neither overwrite user prose nor duplicate it (single write entry,
-- audit/data-model-panorama-gap §五 R1/R2). A document references at most one
-- kp via the partial unique index below; kp.deleted_at lets DELETE /api/kp
-- soft delete a node without breaking its document link (all kp read paths are
-- expected to filter deleted_at, T2.21/W2).
--
-- Backfill: each legacy kp becomes one kind='concept' document (title=kp.name,
-- body_md from gist/detail/trap). The workspace comes from the course's Pod
-- binding (courses.workspace_id, consolidated into 023 for old libraries); it
-- stays NULL when the course has no binding, so W2 can reassign the orphan
-- document.
--
-- No attachments table yet (B5/029) and no FTS triggers (030/B5); v1 lists use
-- LIKE plus the existing /search fallback.
ALTER TABLE kp ADD COLUMN deleted_at TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS knowledge_documents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER REFERENCES workspaces(id) ON DELETE CASCADE,
    course_id    INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    kp_id        INTEGER REFERENCES kp(id) ON DELETE SET NULL,
    kind         TEXT NOT NULL DEFAULT 'note' CHECK(kind IN ('note','concept')),
    title        TEXT NOT NULL,
    body_md      TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived','deleted')),
    version      INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_knowledge_documents_scope
    ON knowledge_documents(workspace_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_knowledge_documents_kp
    ON knowledge_documents(kp_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_documents_course
    ON knowledge_documents(course_id);
-- "one kp owns at most one document" while still allowing unlinked documents.
CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_documents_kp_unique
    ON knowledge_documents(kp_id) WHERE kp_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS document_revisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
    version     INTEGER NOT NULL CHECK(version > 0),
    title       TEXT NOT NULL,
    body_md     TEXT NOT NULL DEFAULT '',
    origin      TEXT NOT NULL DEFAULT 'edit',
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(document_id, version)
);
CREATE INDEX IF NOT EXISTS idx_document_revisions_document
    ON document_revisions(document_id, version DESC);

CREATE TABLE IF NOT EXISTS tags (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER REFERENCES workspaces(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(workspace_id, name)
);
CREATE INDEX IF NOT EXISTS idx_tags_name ON tags(name);

CREATE TABLE IF NOT EXISTS entity_tags (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tag_id      INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL,
    entity_id   INTEGER NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(tag_id, entity_type, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_entity_tags_entity
    ON entity_tags(entity_type, entity_id);

-- Legacy kp -> concept document. INSERT OR IGNORE keeps a replay harmless
-- (the partial unique index on kp_id is the conflict target).
INSERT OR IGNORE INTO knowledge_documents
    (workspace_id, course_id, kp_id, kind, title, body_md, status, version)
SELECT
    c.workspace_id,
    kp.course_id,
    kp.id,
    'concept',
    kp.name,
    TRIM(
        COALESCE(TRIM(kp.gist), '')
        || CASE WHEN TRIM(COALESCE(kp.detail, '')) <> '' THEN
               CASE WHEN TRIM(COALESCE(kp.gist, '')) <> '' THEN char(10) || char(10) ELSE '' END
               || '## 详解' || char(10) || char(10) || TRIM(kp.detail)
           ELSE '' END
        || CASE WHEN TRIM(COALESCE(kp.trap, '')) <> '' THEN
               CASE WHEN TRIM(COALESCE(kp.gist, '')) <> '' OR TRIM(COALESCE(kp.detail, '')) <> ''
                    THEN char(10) || char(10) ELSE '' END
               || '## 易错点' || char(10) || char(10) || TRIM(kp.trap)
           ELSE '' END
    ),
    'active',
    1
FROM kp
LEFT JOIN courses c ON c.id = kp.course_id;

-- Give every backfilled document its initial revision so history is complete
-- and the revision API works on retro documents. NOT EXISTS keeps a replay and
-- user-created documents untouched.
INSERT OR IGNORE INTO document_revisions(document_id, version, title, body_md, origin)
SELECT d.id, 1, d.title, d.body_md, 'backfill'
FROM knowledge_documents d
WHERE d.kp_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM document_revisions r
                  WHERE r.document_id = d.id AND r.version = 1);
