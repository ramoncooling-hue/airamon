"""Editable knowledge documents (026/T2.20): Markdown body, revisions, tags.

Single write entry: document bodies are written here only. kp.detail/gist/trap
stay summaries and a material rescan never touches knowledge_documents, so user
prose is not overwritten by re-indexing (audit/data-model-panorama-gap §五 R1/R2).
Every public write runs in one SQLite transaction; W2 T2.21 exposes the HTTP API.

Delete model: documents are soft deleted (status='deleted', never a physical
row delete); restore flips the status back without inventing a revision. PATCH
updates bump version and always append a document_revisions row, so revision
history is the audit trail (expected_version gives optimistic concurrency).
"""

from __future__ import annotations

import sqlite3

from .db import connect

KINDS = ('note', 'concept')
STATUSES = ('active', 'archived', 'deleted')
MAX_TITLE = 200
MAX_BODY = 2_000_000
MAX_TAG = 40
MAX_TAGS = 32
ENTITY_DOCUMENT = 'document'
ENTITY_KP = 'kp'
ENTITY_COURSE = 'course'
ENTITY_TYPES = (ENTITY_DOCUMENT, ENTITY_KP, ENTITY_COURSE)
_UNSET = object()


def _title(value) -> str:
    text = ' '.join(str(value or '').split()).strip()
    if not text:
        raise ValueError('文档标题不能为空')
    if len(text) > MAX_TITLE:
        raise ValueError(f'文档标题不能超过 {MAX_TITLE} 个字符')
    return text


def _body(value) -> str:
    if value is None:
        return ''
    if not isinstance(value, str):
        raise ValueError('文档正文必须是字符串')
    if len(value) > MAX_BODY:
        raise ValueError('文档正文过长')
    return value


def _kind(value) -> str:
    text = str(value or 'note').strip()
    if text not in KINDS:
        raise ValueError('文档类型只能是 note 或 concept')
    return text


def _status(value) -> str:
    text = str(value or '').strip()
    if text not in STATUSES:
        raise ValueError('文档状态只能是 active、archived 或 deleted')
    return text


def _int_id(value) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError('参数必须是正整数 id') from None
    if parsed <= 0:
        raise ValueError('参数必须是正整数 id')
    return parsed


def _workspace(conn, key) -> dict:
    wanted = ' '.join(str(key or '').split()).strip() or 'studypal'
    row = conn.execute(
        "SELECT id,key FROM workspaces WHERE key=? AND status='active'", (wanted,)).fetchone()
    if not row:
        raise ValueError('工作区不存在')
    return dict(row)


def _course(conn, course_id) -> dict | None:
    if course_id in (None, ''):
        return None
    cid = _int_id(course_id)
    row = conn.execute('SELECT id,name,workspace_id FROM courses WHERE id=?', (cid,)).fetchone()
    if not row:
        raise ValueError('课程不存在')
    return dict(row)


def _kp(conn, kp_id) -> dict | None:
    if kp_id in (None, ''):
        return None
    kid = _int_id(kp_id)
    row = conn.execute(
        'SELECT id,course_id,name,deleted_at FROM kp WHERE id=?', (kid,)).fetchone()
    if not row or str(row['deleted_at'] or '').strip():
        raise ValueError('知识点不存在或已删除')
    return dict(row)


def _link_targets(conn, workspace_id, course_value, kp_value) -> tuple:
    """Validate course/kp links; a kp without an explicit course derives its course."""
    course = _course(conn, course_value)
    kp = _kp(conn, kp_value)
    if kp is not None and course is None:
        course = _course(conn, kp['course_id'])
    if course is not None and course['workspace_id'] is not None:
        if int(course['workspace_id']) != int(workspace_id):
            raise ValueError('课程不属于该工作区')
    if kp is not None and course is not None and int(kp['course_id']) != int(course['id']):
        raise ValueError('知识点不属于该课程')
    return course, kp


def _normalize_tags(tags):
    if tags is None:
        return None
    if isinstance(tags, str):
        tags = [tags]
    try:
        items = iter(tags)
    except TypeError:
        raise ValueError('标签必须是字符串数组') from None
    names = []
    for item in items:
        if not isinstance(item, str):
            raise ValueError('标签必须是字符串数组')
        name = ' '.join(item.split()).strip()
        if not name:
            continue
        if len(name) > MAX_TAG:
            raise ValueError(f'单个标签不能超过 {MAX_TAG} 个字符')
        if name not in names:
            names.append(name)
    if len(names) > MAX_TAGS:
        raise ValueError(f'一篇文档最多 {MAX_TAGS} 个标签')
    return names


def _tag_names(conn, document_id: int) -> list:
    return [row['name'] for row in conn.execute(
        '''SELECT t.name FROM entity_tags et JOIN tags t ON t.id=et.tag_id
           WHERE et.entity_type=? AND et.entity_id=? ORDER BY t.name''',
        (ENTITY_DOCUMENT, document_id)).fetchall()]


def _fetch(conn, document_id: int):
    row = conn.execute(
        'SELECT * FROM knowledge_documents WHERE id=?', (document_id,)).fetchone()
    if not row:
        return None
    document = dict(row)
    document['tags'] = _tag_names(conn, document_id)
    return document


def _set_tags(conn, workspace_id: int, document_id: int, tags) -> None:
    conn.execute('DELETE FROM entity_tags WHERE entity_type=? AND entity_id=?',
                 (ENTITY_DOCUMENT, document_id))
    for name in tags or []:
        conn.execute('INSERT OR IGNORE INTO tags(workspace_id,name) VALUES(?,?)',
                     (workspace_id, name))
        row = conn.execute('SELECT id FROM tags WHERE workspace_id=? AND name=?',
                           (workspace_id, name)).fetchone()
        if row:
            conn.execute(
                'INSERT OR IGNORE INTO entity_tags(tag_id,entity_type,entity_id) VALUES(?,?,?)',
                (row['id'], ENTITY_DOCUMENT, document_id))


def _unique_error(exc: sqlite3.IntegrityError) -> None:
    if 'UNIQUE' in str(exc).upper():
        raise ValueError('该知识点已有关联文档') from exc


def _tag_name(value) -> str:
    name = ' '.join(str(value or '').split()).strip()
    if not name:
        raise ValueError('标签名不能为空')
    if len(name) > MAX_TAG:
        raise ValueError(f'单个标签不能超过 {MAX_TAG} 个字符')
    return name


def _entity_type(value) -> str:
    text = str(value or '').strip()
    if text not in ENTITY_TYPES:
        raise ValueError('实体类型只能是 document、kp 或 course')
    return text


def _tag_ids(conn, tag, workspace_id=None) -> list:
    """Resolve a tag id or name to identifiers, scoped to one workspace."""
    if tag is None or isinstance(tag, bool):
        raise ValueError('标签不存在')
    text = str(tag).strip()
    if not text:
        raise ValueError('标签不存在')
    if isinstance(tag, int) or text.isdigit():
        tag_id = _int_id(tag)
        row = conn.execute('SELECT id,workspace_id FROM tags WHERE id=?', (tag_id,)).fetchone()
        if not row:
            raise ValueError('标签不存在')
        if workspace_id is not None and int(row['workspace_id']) != int(workspace_id):
            raise ValueError('标签不存在')
        return [tag_id]
    name = _tag_name(tag)
    if workspace_id is None:
        rows = conn.execute('SELECT id FROM tags WHERE name=? ORDER BY id', (name,)).fetchall()
    else:
        rows = conn.execute(
            'SELECT id FROM tags WHERE workspace_id=? AND name=? ORDER BY id',
            (int(workspace_id), name)).fetchall()
    if not rows:
        raise ValueError('标签不存在')
    return [int(row['id']) for row in rows]


def _tag_row(conn, tag, workspace_id) -> dict:
    """One existing tag of a workspace; unknown tags are 404 at the API level."""
    row = conn.execute('SELECT id,name,workspace_id FROM tags WHERE id=?',
                       (_tag_ids(conn, tag, workspace_id)[0],)).fetchone()
    if not row:
        raise ValueError('标签不存在')
    return dict(row)


def _entity_context(conn, entity_type, entity_id) -> dict:
    """Validate a live document/kp/course and return its workspace and title."""
    if entity_type == ENTITY_DOCUMENT:
        row = conn.execute(
            "SELECT id,title,workspace_id FROM knowledge_documents "
            "WHERE id=? AND status<>'deleted'", (entity_id,)).fetchone()
        if not row:
            raise ValueError('文档不存在或已删除')
        return dict(row)
    if entity_type == ENTITY_KP:
        from . import repo as repo_module
        kp = repo_module.require_live_kp(entity_id)
        return {'id': int(kp['id']), 'title': kp['name'],
                'workspace_id': kp.get('workspace_id')}
    row = conn.execute('SELECT id,name,workspace_id FROM courses WHERE id=?',
                       (entity_id,)).fetchone()
    if not row:
        raise ValueError('课程不存在')
    return {'id': int(row['id']), 'title': row['name'],
            'workspace_id': row['workspace_id']}


def _tag_workspace(conn, entity, workspace_key) -> int:
    """A tag entity must live in the tagged entity's workspace."""
    if workspace_key not in (None, ''):
        requested = _workspace(conn, workspace_key)['id']
        if entity['workspace_id'] is not None and int(requested) != int(entity['workspace_id']):
            raise ValueError('实体不属于该工作区')
        return int(requested)
    if entity['workspace_id'] is not None:
        return int(entity['workspace_id'])
    return int(_workspace(conn, None)['id'])
    raise


def create(workspace_key, *, title, body_md='', kind='note', course_id=None, kp_id=None,
           tags=None, origin='manual'):
    """Create one document plus its first revision and tags."""
    conn = connect()
    try:
        workspace = _workspace(conn, workspace_key)
        clean_title = _title(title)
        clean_body = _body(body_md)
        clean_kind = _kind(kind)
        clean_tags = _normalize_tags(tags)
        course, kp = _link_targets(conn, workspace['id'], course_id, kp_id)
        try:
            with conn:
                cursor = conn.execute(
                    '''INSERT INTO knowledge_documents
                       (workspace_id,course_id,kp_id,kind,title,body_md,status,version)
                       VALUES(?,?,?,?,?,?,'active',1)''',
                    (workspace['id'], course['id'] if course else None,
                     kp['id'] if kp else None, clean_kind, clean_title, clean_body))
                document_id = cursor.lastrowid
                conn.execute(
                    '''INSERT INTO document_revisions(document_id,version,title,body_md,origin)
                       VALUES(?,1,?,?,?)''',
                    (document_id, clean_title, clean_body, str(origin or 'manual')[:40]))
                _set_tags(conn, workspace['id'], document_id, clean_tags)
        except sqlite3.IntegrityError as exc:
            _unique_error(exc)
        return _fetch(conn, document_id)
    finally:
        conn.close()


def ensure_imported_wiki_document(conn, *, workspace_id: int, course_id: int,
                          kp_id: int, title: str, body_md: str) -> None:
    """Create/update an importer-owned document without replacing user prose."""
    cursor = conn.execute(
        '''INSERT OR IGNORE INTO knowledge_documents
           (workspace_id,course_id,kp_id,kind,title,body_md,status,version)
           VALUES(?,?,?,'concept',?,?,'active',1)''',
        (workspace_id, course_id, kp_id, title, body_md))
    if cursor.rowcount:
        document_id = int(cursor.lastrowid)
        conn.execute('''INSERT INTO document_revisions(document_id,version,title,body_md,origin)
                        VALUES(?,1,?,?,'wiki_import')''',
                     (document_id, title, body_md))
        return
    row = conn.execute(
        "SELECT * FROM knowledge_documents WHERE kp_id=? AND status<>'deleted'", (kp_id,)
    ).fetchone()
    if not row:
        return
    revision = conn.execute(
        'SELECT origin FROM document_revisions WHERE document_id=? ORDER BY version DESC LIMIT 1',
        (row['id'],)).fetchone()
    # Only this importer owns updates. Historical backfills have uncertain
    # provenance and are preserved along with all user revisions.
    if not revision or revision['origin'] != 'wiki_import':
        return
    if row['title'] == title and row['body_md'] == body_md:
        return
    version = int(row['version']) + 1
    conn.execute('''UPDATE knowledge_documents SET title=?,body_md=?,version=?,
                    updated_at=datetime('now','localtime') WHERE id=?''',
                 (title, body_md, version, row['id']))
    conn.execute('''INSERT INTO document_revisions(document_id,version,title,body_md,origin)
                    VALUES(?,?,?,?,'wiki_import')''',
                 (row['id'], version, title, body_md))


def get(document_id):
    conn = connect()
    try:
        return _fetch(conn, _int_id(document_id))
    finally:
        conn.close()


def find_by_kp(kp_id):
    """The one document linked to a kp (026 guarantees at most one)."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id FROM knowledge_documents WHERE kp_id=? AND status<>'deleted'",
            (_int_id(kp_id),)).fetchone()
        return _fetch(conn, row['id']) if row else None
    finally:
        conn.close()


def list(workspace=None, course=None, kind=None, status='active', q=None,
         limit=50, offset=0, tag=None):
    """List documents; status='all' includes archived and deleted rows."""
    conn = connect()
    try:
        sql = 'SELECT * FROM knowledge_documents WHERE 1=1'
        args = []
        workspace_id = None
        if workspace:
            workspace_id = _workspace(conn, workspace)['id']
            args.append(workspace_id)
            sql += ' AND workspace_id=?'
        if course not in (None, ''):
            args.append(_int_id(course))
            sql += ' AND course_id=?'
        if kind:
            args.append(_kind(kind))
            sql += ' AND kind=?'
        if status and str(status) != 'all':
            args.append(_status(status))
            sql += ' AND status=?'
        query = ' '.join(str(q or '').split()).strip()
        if query:
            pattern = '%' + query + '%'
            sql += ' AND (title LIKE ? OR body_md LIKE ?)'
            args.extend([pattern, pattern])
        if tag not in (None, ''):
            tag_ids = _tag_ids(conn, tag, workspace_id)
            marks = ','.join('?' for _ in tag_ids)
            sql += (f' AND id IN (SELECT entity_id FROM entity_tags'
                    f" WHERE entity_type=? AND tag_id IN ({marks}))")
            args.append(ENTITY_DOCUMENT)
            args.extend(tag_ids)
        sql += ' ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?'
        args.extend([max(1, min(int(limit or 50), 200)), max(0, int(offset or 0))])
        documents = [dict(row) for row in conn.execute(sql, args).fetchall()]
        for document in documents:
            document['tags'] = _tag_names(conn, document['id'])
        return documents
    finally:
        conn.close()


def update(document_id, *, title=None, body_md=None, kind=None, course_id=_UNSET,
           kp_id=_UNSET, status=None, tags=None, expected_version=None, origin='manual'):
    """Patch fields and append a revision (version+1); tags-only patches keep the version."""
    conn = connect()
    try:
        document_id = _int_id(document_id)
        row = conn.execute(
            'SELECT * FROM knowledge_documents WHERE id=?', (document_id,)).fetchone()
        if not row:
            raise ValueError('文档不存在')
        current = dict(row)
        if current['status'] == 'deleted':
            raise ValueError('文档已删除，请先恢复')
        if expected_version is not None and int(expected_version) != int(current['version']):
            raise ValueError('文档版本冲突，请刷新后重试')
        clean_title = _title(title) if title is not None else current['title']
        clean_body = _body(body_md) if body_md is not None else current['body_md']
        clean_kind = _kind(kind) if kind is not None else current['kind']
        clean_status = _status(status) if status is not None else current['status']
        if clean_status == 'deleted':
            raise ValueError('删除文档请使用软删除')
        clean_tags = _normalize_tags(tags)
        course, kp = _link_targets(
            conn, current['workspace_id'],
            current['course_id'] if course_id is _UNSET else course_id,
            current['kp_id'] if kp_id is _UNSET else kp_id)
        next_course = course['id'] if course else None
        next_kp = kp['id'] if kp else None
        content_changed = (clean_title != current['title'] or clean_body != current['body_md']
                           or clean_kind != current['kind'] or clean_status != current['status']
                           or next_course != current['course_id'] or next_kp != current['kp_id'])
        try:
            with conn:
                if content_changed:
                    version = int(current['version']) + 1
                    cursor = conn.execute(
                        '''UPDATE knowledge_documents
                           SET title=?,body_md=?,kind=?,course_id=?,kp_id=?,status=?,version=?,
                               updated_at=datetime('now','localtime')
                           WHERE id=? AND version=?''',
                        (clean_title, clean_body, clean_kind, next_course, next_kp,
                         clean_status, version, document_id, current['version']))
                    if cursor.rowcount != 1:
                        raise ValueError('文档版本冲突，请刷新后重试')
                    conn.execute(
                        '''INSERT INTO document_revisions(document_id,version,title,body_md,origin)
                           VALUES(?,?,?,?,?)''',
                        (document_id, version, clean_title, clean_body,
                         str(origin or 'manual')[:40]))
                else:
                    conn.execute(
                        """UPDATE knowledge_documents
                           SET updated_at=datetime('now','localtime') WHERE id=?""",
                        (document_id,))
                if clean_tags is not None:
                    _set_tags(conn, current['workspace_id'], document_id, clean_tags)
        except sqlite3.IntegrityError as exc:
            _unique_error(exc)
        return _fetch(conn, document_id)
    finally:
        conn.close()


def soft_delete(document_id, *, expected_version=None):
    """status='deleted'; rows and revisions are kept (physical delete is external)."""
    conn = connect()
    try:
        document_id = _int_id(document_id)
        row = conn.execute(
            'SELECT * FROM knowledge_documents WHERE id=?', (document_id,)).fetchone()
        if not row:
            raise ValueError('文档不存在')
        current = dict(row)
        if current['status'] == 'deleted':
            return _fetch(conn, document_id)
        if expected_version is not None and int(expected_version) != int(current['version']):
            raise ValueError('文档版本冲突，请刷新后重试')
        with conn:
            conn.execute(
                """UPDATE knowledge_documents
                   SET status='deleted', updated_at=datetime('now','localtime') WHERE id=?""",
                (document_id,))
        return _fetch(conn, document_id)
    finally:
        conn.close()


def restore(document_id):
    conn = connect()
    try:
        document_id = _int_id(document_id)
        row = conn.execute(
            'SELECT * FROM knowledge_documents WHERE id=?', (document_id,)).fetchone()
        if not row:
            raise ValueError('文档不存在')
        if row['status'] != 'deleted':
            return _fetch(conn, document_id)
        with conn:
            conn.execute(
                """UPDATE knowledge_documents
                   SET status='active', updated_at=datetime('now','localtime') WHERE id=?""",
                (document_id,))
        return _fetch(conn, document_id)
    finally:
        conn.close()


def list_revisions(document_id):
    conn = connect()
    try:
        document_id = _int_id(document_id)
        if not conn.execute('SELECT 1 FROM knowledge_documents WHERE id=?',
                            (document_id,)).fetchone():
            raise ValueError('文档不存在')
        return [dict(row) for row in conn.execute(
            '''SELECT id,document_id,version,title,body_md,origin,created_at
               FROM document_revisions WHERE document_id=? ORDER BY version DESC''',
            (document_id,)).fetchall()]
    finally:
        conn.close()


def restore_revision(document_id, version):
    """Restore an old revision as a new revision (version+1); history is kept."""
    conn = connect()
    try:
        document_id = _int_id(document_id)
        version = _int_id(version)
        row = conn.execute(
            'SELECT * FROM knowledge_documents WHERE id=?', (document_id,)).fetchone()
        if not row:
            raise ValueError('文档不存在')
        current = dict(row)
        if current['status'] == 'deleted':
            raise ValueError('文档已删除，请先恢复')
        revision = conn.execute(
            'SELECT * FROM document_revisions WHERE document_id=? AND version=?',
            (document_id, version)).fetchone()
        if not revision:
            raise ValueError('修订版本不存在')
        next_version = int(current['version']) + 1
        with conn:
            cursor = conn.execute(
                '''UPDATE knowledge_documents
                   SET title=?,body_md=?,version=?,updated_at=datetime('now','localtime')
                   WHERE id=? AND version=?''',
                (revision['title'], revision['body_md'], next_version,
                 document_id, current['version']))
            if cursor.rowcount != 1:
                raise ValueError('文档版本冲突，请刷新后重试')
            conn.execute(
                '''INSERT INTO document_revisions(document_id,version,title,body_md,origin)
                   VALUES(?,?,?,?,?)''',
                (document_id, next_version, revision['title'], revision['body_md'],
                 f"rollback:v{version}"))
        return _fetch(conn, document_id)
    finally:
        conn.close()


def set_tags(document_id, tags):
    conn = connect()
    try:
        document_id = _int_id(document_id)
        row = conn.execute(
            'SELECT id,workspace_id FROM knowledge_documents WHERE id=?',
            (document_id,)).fetchone()
        if not row:
            raise ValueError('文档不存在')
        clean = _normalize_tags(tags)
        with conn:
            _set_tags(conn, row['workspace_id'], document_id, clean or [])
        return _tag_names(conn, document_id)
    finally:
        conn.close()


def get_tags(document_id):
    conn = connect()
    try:
        document_id = _int_id(document_id)
        if not conn.execute('SELECT 1 FROM knowledge_documents WHERE id=?',
                            (document_id,)).fetchone():
            raise ValueError('文档不存在')
        return _tag_names(conn, document_id)
    finally:
        conn.close()


def list_tags(workspace_key=None):
    """Tags of one workspace with live usage counts (document/kp/course)."""
    conn = connect()
    try:
        workspace = _workspace(conn, workspace_key)
        rows = [dict(row) for row in conn.execute(
            """SELECT t.id,t.name,
                      (SELECT COUNT(*) FROM entity_tags et WHERE et.tag_id=t.id AND (
                          (et.entity_type='document' AND EXISTS(
                              SELECT 1 FROM knowledge_documents d
                               WHERE d.id=et.entity_id AND d.status<>'deleted'))
                       OR (et.entity_type='kp' AND EXISTS(
                              SELECT 1 FROM kp k
                               WHERE k.id=et.entity_id AND COALESCE(k.deleted_at,'')=''))
                       OR (et.entity_type='course' AND EXISTS(
                              SELECT 1 FROM courses c WHERE c.id=et.entity_id))))
                      AS count,
                      (SELECT COUNT(*) FROM entity_tags et
                         JOIN knowledge_documents d ON d.id=et.entity_id
                        WHERE et.tag_id=t.id AND et.entity_type='document'
                          AND d.status<>'deleted') AS document_count,
                      (SELECT COUNT(*) FROM entity_tags et JOIN kp k ON k.id=et.entity_id
                        WHERE et.tag_id=t.id AND et.entity_type='kp'
                          AND COALESCE(k.deleted_at,'')='') AS kp_count,
                      (SELECT COUNT(*) FROM entity_tags et JOIN courses c ON c.id=et.entity_id
                        WHERE et.tag_id=t.id AND et.entity_type='course') AS course_count
                 FROM tags t WHERE t.workspace_id=? ORDER BY t.name""",
            (workspace['id'],)).fetchall()]
        for row in rows:
            row['entity_types'] = {
                ENTITY_DOCUMENT: int(row.pop('document_count')),
                ENTITY_KP: int(row.pop('kp_count')),
                ENTITY_COURSE: int(row.pop('course_count')),
            }
            row['document_count'] = row['entity_types'][ENTITY_DOCUMENT]
            row['count'] = int(row['count'])
        return rows
    finally:
        conn.close()


def ensure_tag(workspace_key, name):
    """Create a tag in one workspace or return the existing one (idempotent)."""
    conn = connect()
    try:
        workspace = _workspace(conn, workspace_key)
        clean = _tag_name(name)
        with conn:
            cursor = conn.execute('INSERT OR IGNORE INTO tags(workspace_id,name) VALUES(?,?)',
                                  (workspace['id'], clean))
            created = cursor.rowcount == 1
            row = conn.execute('SELECT id,name,workspace_id FROM tags WHERE workspace_id=? AND name=?',
                               (workspace['id'], clean)).fetchone()
        return {**dict(row), 'created': created}
    finally:
        conn.close()


def delete_tag(tag_id):
    """Delete a tag; entity_tags rows cascade with it so links disappear."""
    conn = connect()
    try:
        tag_id = _int_id(tag_id)
        row = conn.execute('SELECT id,name,workspace_id FROM tags WHERE id=?',
                           (tag_id,)).fetchone()
        if not row:
            raise ValueError('标签不存在')
        with conn:
            conn.execute('DELETE FROM tags WHERE id=?', (tag_id,))
        return {**dict(row), 'removed': True}
    finally:
        conn.close()


def attach_entity(tag, entity_type, entity_id, workspace_key=None):
    """Attach an existing tag to a live document/kp/course (idempotent)."""
    conn = connect()
    try:
        kind = _entity_type(entity_type)
        entity = _entity_context(conn, kind, _int_id(entity_id))
        workspace_id = _tag_workspace(conn, entity, workspace_key)
        target = _tag_row(conn, tag, workspace_id)
        with conn:
            cursor = conn.execute(
                'INSERT OR IGNORE INTO entity_tags(tag_id,entity_type,entity_id) VALUES(?,?,?)',
                (target['id'], kind, entity['id']))
            created = cursor.rowcount == 1
        return {'tag': {'id': target['id'], 'name': target['name']},
                'entity_type': kind, 'entity_id': entity['id'],
                'title': entity['title'], 'created': created}
    finally:
        conn.close()


def detach_entity(tag, entity_type, entity_id, workspace_key=None):
    """Remove one tag link; detaching a missing link is a no-op (removed=False)."""
    conn = connect()
    try:
        kind = _entity_type(entity_type)
        entity = _entity_context(conn, kind, _int_id(entity_id))
        workspace_id = _tag_workspace(conn, entity, workspace_key)
        target = _tag_row(conn, tag, workspace_id)
        with conn:
            cursor = conn.execute(
                'DELETE FROM entity_tags WHERE tag_id=? AND entity_type=? AND entity_id=?',
                (target['id'], kind, entity['id']))
            removed = cursor.rowcount > 0
        return {'tag': {'id': target['id'], 'name': target['name']},
                'entity_type': kind, 'entity_id': entity['id'], 'removed': removed}
    finally:
        conn.close()


def entities_of_tag(tag_id):
    """Live entities carrying a tag, grouped by entity type with counts."""
    conn = connect()
    try:
        tag_id = _int_id(tag_id)
        tag_row = conn.execute('SELECT id,name,workspace_id FROM tags WHERE id=?',
                               (tag_id,)).fetchone()
        if not tag_row:
            raise ValueError('标签不存在')
        rows = conn.execute(
            """SELECT 'document' AS entity_type, d.id AS entity_id, d.title AS title,
                      d.status AS status
                 FROM entity_tags et JOIN knowledge_documents d ON d.id=et.entity_id
                WHERE et.tag_id=? AND et.entity_type='document' AND d.status<>'deleted'
               UNION ALL
               SELECT 'kp', k.id, k.name, 'active'
                 FROM entity_tags et JOIN kp k ON k.id=et.entity_id
                WHERE et.tag_id=? AND et.entity_type='kp' AND COALESCE(k.deleted_at,'')=''
               UNION ALL
               SELECT 'course', c.id, c.name, 'active'
                 FROM entity_tags et JOIN courses c ON c.id=et.entity_id
                WHERE et.tag_id=? AND et.entity_type='course'
                ORDER BY 1, 2""",
            (tag_id, tag_id, tag_id)).fetchall()
        entities = [dict(row) for row in rows]
        counts = {ENTITY_DOCUMENT: 0, ENTITY_KP: 0, ENTITY_COURSE: 0}
        for entity in entities:
            counts[entity['entity_type']] = counts.get(entity['entity_type'], 0) + 1
        counts['total'] = len(entities)
        return {'tag': dict(tag_row), 'entities': entities, 'counts': counts}
    finally:
        conn.close()


def tagged_refs(tag, workspace_key=None):
    """{entity_type: {str(entity_id)}} for live entities carrying one tag."""
    conn = connect()
    try:
        workspace_id = None
        if workspace_key not in (None, ''):
            workspace_id = _workspace(conn, workspace_key)['id']
        tag_ids = _tag_ids(conn, tag, workspace_id)
        marks = ','.join('?' for _ in tag_ids)
        sql = f"""SELECT 'document' AS entity_type, d.id AS entity_id
                    FROM entity_tags et JOIN knowledge_documents d ON d.id=et.entity_id
                   WHERE et.tag_id IN ({marks}) AND et.entity_type=?
                     AND d.status<>'deleted'
                  UNION ALL
                  SELECT 'kp', k.id
                    FROM entity_tags et JOIN kp k ON k.id=et.entity_id
                   WHERE et.tag_id IN ({marks}) AND et.entity_type=?
                     AND COALESCE(k.deleted_at,'')=''
                  UNION ALL
                  SELECT 'course', c.id
                    FROM entity_tags et JOIN courses c ON c.id=et.entity_id
                   WHERE et.tag_id IN ({marks}) AND et.entity_type=?"""
        args = (tag_ids + [ENTITY_DOCUMENT] + tag_ids + [ENTITY_KP]
                + tag_ids + [ENTITY_COURSE])
        refs = {ENTITY_DOCUMENT: set(), ENTITY_KP: set(), ENTITY_COURSE: set()}
        for row in conn.execute(sql, args):
            refs.setdefault(str(row['entity_type']), set()).add(str(row['entity_id']))
        return refs
    finally:
        conn.close()


def documents_by_tag(tag, workspace_key=None, course=None, kind=None, status='active',
                     q=None, limit=50, offset=0):
    """Documents carrying a tag; thin wrapper over list(tag=...)."""
    return list(workspace=workspace_key, course=course, kind=kind, status=status,
                q=q, limit=limit, offset=offset, tag=tag)
