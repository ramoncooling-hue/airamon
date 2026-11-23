"""CyberPal · 记忆层查询。

只回答「记忆怎么取」，不含业务规则。
三条判别线的落点：
    检索即记忆 → search() 统一入口
    回答带引用 → 每条返回都带 source
    结论能写回 → write_mastery() 且必留痕
"""

from __future__ import annotations

import json
import hashlib
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from .db import connect, DATA_DIR
from . import chapter_catalog, domain_profiles, wiki

logger = logging.getLogger(__name__)


def _workspace_id(key: str | None = None) -> int | None:
    wanted = (key or 'studypal').strip() or 'studypal'
    conn = connect()
    try:
        row = conn.execute('SELECT id FROM workspaces WHERE key=? AND status="active"', (wanted,)).fetchone()
        return int(row['id']) if row else None
    finally:
        conn.close()


def db_workspace_folder(key: str) -> Path:
    return DATA_DIR / ('StudyPal' if key == 'studypal' else str(key))


def list_workspaces() -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT w.*,
                      (SELECT COUNT(*) FROM courses c WHERE c.workspace_id=w.id OR (c.workspace_id IS NULL AND w.key='studypal')) AS course_count,
                      (SELECT COUNT(*) FROM workspace_items i WHERE i.workspace_id=w.id) AS item_count,
                      (SELECT COUNT(*) FROM events e WHERE e.workspace_id=w.id OR (e.workspace_id IS NULL AND w.key='studypal')) AS event_count
                 FROM workspaces w WHERE w.status='active' ORDER BY w.sort_order,w.id"""
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_workspace(key: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM workspaces WHERE key=? AND status='active'", (key,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_workspace(key: str, name: str, *, kind: str = 'custom', domain: str = '',
                     description: str = '') -> dict:
    key = re.sub(r'[^a-z0-9_-]+', '-', (key or '').strip().lower()).strip('-_')
    name = ' '.join((name or '').split()).strip()
    if not key or len(key) > 48:
        raise ValueError('工作区标识只能包含小写字母、数字、下划线或短横线')
    if not name or len(name) > 80:
        raise ValueError('工作区名称不能为空且不能超过 80 个字符')
    if key in {'app', 'shared', 'studypal'} and key != 'studypal':
        raise ValueError('这个工作区标识不可用')
    conn = connect()
    try:
        order = conn.execute('SELECT COALESCE(MAX(sort_order),-1)+1 AS n FROM workspaces').fetchone()['n']
        cur = conn.execute(
            """INSERT INTO workspaces(key,name,kind,domain,description,sort_order)
               VALUES(?,?,?,?,?,?)""",
            (key, name, (kind or 'custom').strip()[:24], (domain or '').strip()[:48],
             (description or '').strip()[:240], order),
        )
        conn.commit()
        return dict(conn.execute('SELECT * FROM workspaces WHERE id=?', (cur.lastrowid,)).fetchone())
    except Exception as exc:
        conn.rollback()
        if 'UNIQUE' in str(exc).upper():
            raise ValueError('工作区标识已存在') from exc
        raise
    finally:
        conn.close()


def list_workspace_items(workspace: str, kind: str | None = None) -> list[dict]:
    wid = _workspace_id(workspace)
    if wid is None:
        return []
    conn = connect()
    try:
        sql = 'SELECT * FROM workspace_items WHERE workspace_id=?'
        args: list = [wid]
        if kind:
            sql += ' AND kind=?'; args.append(kind)
        sql += ' ORDER BY CASE WHEN status="open" THEN 0 ELSE 1 END, COALESCE(due_at,""), updated_at DESC, id DESC'
        return [dict(row) for row in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def create_workspace_item(workspace: str, values: dict) -> dict:
    wid = _workspace_id(workspace)
    if wid is None:
        raise ValueError('工作区不存在')
    title = ' '.join(str(values.get('title') or '').split()).strip()
    if not title:
        raise ValueError('记录标题不能为空')
    kind = str(values.get('kind') or 'note').strip()
    if kind not in {'note', 'task'}:
        raise ValueError('记录类型只能是 note 或 task')
    status = str(values.get('status') or 'open').strip()
    if status not in {'open', 'done', 'archived'}:
        raise ValueError('记录状态无效')
    conn = connect()
    try:
        cur = conn.execute(
            """INSERT INTO workspace_items(workspace_id,kind,title,body,status,due_at)
               VALUES(?,?,?,?,?,?)""",
            (wid, kind, title[:180], str(values.get('body') or '')[:10000], status,
             str(values.get('due_at') or '').strip()[:40]),
        )
        row = dict(conn.execute('SELECT i.*,w.key AS workspace_key FROM workspace_items i JOIN workspaces w ON w.id=i.workspace_id WHERE i.id=?', (cur.lastrowid,)).fetchone())
        if kind in {'note', 'task'}:
            folder = db_workspace_folder(row['workspace_key']) / 'notes'
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{row['id']}-{wiki._slug(title)}.md"
            path.write_text(wiki.render_workspace_note(workspace=row['workspace_key'], title=title, body=row['body'], kind=kind, created=row['created_at'], updated=row['updated_at'], status=row['status'], due_at=row['due_at'], source=f"workspace-items/{row['id']}"), encoding='utf-8')
            conn.execute('UPDATE workspace_items SET path=? WHERE id=?', (str(path), row['id']))
            row['path'] = str(path)
        conn.commit()
        return row
    finally:
        conn.close()


def update_workspace_item(item_id: int, values: dict) -> dict:
    allowed = {'title', 'body', 'status', 'due_at'}
    patch = {key: values[key] for key in values if key in allowed}
    if 'title' in patch and not str(patch['title']).strip():
        raise ValueError('记录标题不能为空')
    if 'kind' in patch and patch['kind'] not in {'note', 'task'}:
        raise ValueError('记录类型只能是 note 或 task')
    if 'status' in patch and patch['status'] not in {'open', 'done', 'archived'}:
        raise ValueError('记录状态无效')
    if not patch:
        raise ValueError('没有可修改的记录字段')
    conn = connect()
    try:
        if not conn.execute('SELECT id FROM workspace_items WHERE id=?', (item_id,)).fetchone():
            raise ValueError('工作区记录不存在')
        sets = ','.join(f'{key}=?' for key in patch)
        conn.execute(f"UPDATE workspace_items SET {sets},updated_at=datetime('now','localtime') WHERE id=?",
                     [*patch.values(), item_id])
        row = dict(conn.execute('''SELECT i.*,w.key AS workspace_key FROM workspace_items i
                                   JOIN workspaces w ON w.id=i.workspace_id WHERE i.id=?''', (item_id,)).fetchone())
        if row.get('path'):
            Path(row['path']).write_text(wiki.render_workspace_note(
                workspace=row['workspace_key'], title=row['title'], body=row['body'],
                kind=row['kind'], created=row['created_at'], updated=row['updated_at'],
                status=row['status'], due_at=row['due_at'], source=f"workspace-items/{row['id']}"), encoding='utf-8')
        conn.commit()
        return row
    finally:
        conn.close()


def delete_workspace_item(item_id: int) -> bool:
    conn = connect()
    try:
        row = conn.execute('SELECT i.path,w.key FROM workspace_items i JOIN workspaces w ON w.id=i.workspace_id WHERE i.id=?', (item_id,)).fetchone()
        if row and row['path']:
            path = Path(row['path']).resolve()
            if not path.is_relative_to((db_workspace_folder(row['key']) / 'notes').resolve()):
                raise ValueError('记录路径不属于当前工作区')
            path.unlink(missing_ok=True)
        cur = conn.execute('DELETE FROM workspace_items WHERE id=?', (item_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


MEMORY_LAYERS = ('identity', 'preference', 'rule', 'episodic', 'semantic', 'working')
MEMORY_STATUSES = ('draft', 'active', 'archived', 'superseded')
_MEMORY_LAYER_PRIORITY = {
    'identity': 0, 'preference': 1, 'rule': 2,
    'semantic': 3, 'episodic': 4, 'working': 5,
}


def _validate_memory_values(values: dict, *, partial: bool = False) -> dict:
    fields = {'layer', 'title', 'content', 'status', 'source_type', 'source_id', 'confidence', 'confirmed'}
    patch = {key: values[key] for key in values if key in fields}
    if not partial or 'layer' in patch:
        layer = str(patch.get('layer', '')).strip()
        if layer not in MEMORY_LAYERS:
            raise ValueError('记忆层只能是 identity / preference / rule / episodic / semantic / working')
        patch['layer'] = layer
    if not partial or 'title' in patch:
        title = ' '.join(str(patch.get('title', '')).split()).strip()
        if not title:
            raise ValueError('记忆标题不能为空')
        if len(title) > 180:
            raise ValueError('记忆标题不能超过 180 个字符')
        patch['title'] = title
    if 'content' in patch:
        patch['content'] = str(patch['content'] or '').strip()[:20000]
    if 'status' in patch:
        patch['status'] = str(patch['status'] or '').strip()
        if patch['status'] not in MEMORY_STATUSES:
            raise ValueError('记忆状态无效')
    if 'source_type' in patch:
        patch['source_type'] = str(patch['source_type'] or 'manual').strip()[:80]
    if 'source_id' in patch:
        patch['source_id'] = str(patch['source_id'] or '').strip()[:180]
    if 'confidence' in patch:
        try:
            patch['confidence'] = max(0.0, min(1.0, float(patch['confidence'])))
        except (TypeError, ValueError) as exc:
            raise ValueError('记忆置信度必须是 0 到 1 之间的数字') from exc
    if 'confirmed' in patch:
        patch['confirmed'] = 1 if bool(patch['confirmed']) else 0
    return patch


def _memory_row(conn, memory_id: int):
    return conn.execute('SELECT * FROM memory_blocks WHERE id=?', (memory_id,)).fetchone()


def _write_memory_revision(conn, row: dict, *, origin: str) -> None:
    conn.execute(
        '''INSERT INTO memory_block_revisions
           (memory_block_id,version,layer,title,content,status,source_type,source_id,confidence,confirmed,origin)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
        (row['id'], row['version'], row['layer'], row['title'], row['content'], row['status'],
         row['source_type'], row['source_id'], row['confidence'], row['confirmed'], origin[:80]),
    )


def list_memory_blocks(workspace: str = 'studypal', *, layer: str | None = None,
                       status: str | None = 'active', limit: int = 100) -> list[dict]:
    if layer and layer not in MEMORY_LAYERS:
        raise ValueError('记忆层无效')
    if status and status not in MEMORY_STATUSES:
        raise ValueError('记忆状态无效')
    limit = max(1, min(int(limit), 500))
    sql = 'SELECT * FROM memory_blocks WHERE workspace_key=?'
    args: list = [workspace]
    if layer:
        sql += ' AND layer=?'; args.append(layer)
    if status:
        sql += ' AND status=?'; args.append(status)
    sql += ' ORDER BY CASE layer WHEN \'identity\' THEN 0 WHEN \'preference\' THEN 1 WHEN \'rule\' THEN 2 WHEN \'semantic\' THEN 3 WHEN \'episodic\' THEN 4 ELSE 5 END, updated_at DESC, id DESC LIMIT ?'
    args.append(limit)
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def get_memory_block(memory_id: int) -> dict | None:
    conn = connect()
    try:
        row = _memory_row(conn, memory_id)
        if not row:
            return None
        item = dict(row)
        item['revisions'] = [dict(revision) for revision in conn.execute(
            'SELECT * FROM memory_block_revisions WHERE memory_block_id=? ORDER BY version DESC',
            (memory_id,),
        ).fetchall()]
        return item
    finally:
        conn.close()


def create_memory_block(workspace: str, values: dict, *, origin: str = 'manual') -> dict:
    if not get_workspace(workspace):
        raise ValueError('工作区不存在')
    patch = _validate_memory_values(values)
    patch.setdefault('content', '')
    patch.setdefault('status', 'active')
    patch.setdefault('source_type', 'manual')
    patch.setdefault('source_id', '')
    patch.setdefault('confidence', 0.8)
    patch.setdefault('confirmed', 1)
    conn = connect()
    try:
        cur = conn.execute(
            '''INSERT INTO memory_blocks
               (workspace_key,layer,title,content,status,source_type,source_id,confidence,confirmed)
               VALUES(?,?,?,?,?,?,?,?,?)''',
            (workspace, patch['layer'], patch['title'], patch['content'], patch['status'],
             patch['source_type'], patch['source_id'], patch['confidence'], patch['confirmed']),
        )
        row = dict(_memory_row(conn, cur.lastrowid))
        _write_memory_revision(conn, row, origin=origin)
        conn.commit()
        return row
    finally:
        conn.close()


def update_memory_block(memory_id: int, values: dict, *, origin: str = 'manual') -> dict:
    patch = _validate_memory_values(values, partial=True)
    if not patch:
        raise ValueError('没有可修改的记忆字段')
    conn = connect()
    try:
        old = _memory_row(conn, memory_id)
        if not old:
            raise ValueError('记忆不存在')
        changed = any(str(old[key]) != str(value) for key, value in patch.items())
        if not changed:
            return {'ok': True, 'changed': [], 'memory': dict(old)}
        version = int(old['version']) + 1
        sets = ','.join(f'{key}=?' for key in patch)
        values_list = list(patch.values())
        conn.execute(
            f"UPDATE memory_blocks SET {sets}, version=?, updated_at=datetime('now','localtime') WHERE id=?",
            [*values_list, version, memory_id],
        )
        row = dict(_memory_row(conn, memory_id))
        _write_memory_revision(conn, row, origin=origin)
        conn.commit()
        return {'ok': True, 'changed': [key for key in patch if str(old[key]) != str(patch[key])], 'memory': row}
    finally:
        conn.close()


def archive_memory_block(memory_id: int, *, origin: str = 'manual') -> dict:
    return update_memory_block(memory_id, {'status': 'archived'}, origin=origin)


def list_memory_revisions(memory_id: int) -> list[dict]:
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(
            'SELECT * FROM memory_block_revisions WHERE memory_block_id=? ORDER BY version DESC',
            (memory_id,),
        ).fetchall()]
    finally:
        conn.close()


def search_memory_blocks(query: str = '', limit: int = 12, workspace: str = 'studypal',
                         *, include_stable: bool = True) -> list[dict]:
    limit = max(1, min(int(limit), 100))
    terms = keywords_of(query) if query else []
    conn = connect()
    try:
        sql = 'SELECT * FROM memory_blocks WHERE workspace_key=? AND status=\'active\''
        args: list = [workspace]
        if terms:
            clauses = ['(title LIKE ? OR content LIKE ?)'] * len(terms)
            args.extend(value for term in terms for value in (f'%{term}%', f'%{term}%'))
            stable = "layer IN ('identity','preference','rule')" if include_stable else '0'
            sql += ' AND (' + ' OR '.join(clauses) + ' OR ' + stable + ')'
        sql += ' ORDER BY confirmed DESC, CASE layer WHEN \'identity\' THEN 0 WHEN \'preference\' THEN 1 WHEN \'rule\' THEN 2 WHEN \'semantic\' THEN 3 WHEN \'episodic\' THEN 4 ELSE 5 END, updated_at DESC, id DESC LIMIT ?'
        args.append(limit)
        return [dict(row) for row in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def list_courses(workspace: str | None = None) -> list[dict]:
    conn = connect()
    try:
        sql = """SELECT c.*, COALESCE(w.key,'studypal') AS workspace_key,
                         COALESCE(w.name,'StudyPal') AS workspace_name,
                         (SELECT COUNT(*) FROM kp WHERE course_id=c.id
                            AND COALESCE(deleted_at,'')='') AS kp_count
                    FROM courses c LEFT JOIN workspaces w ON w.id=c.workspace_id"""
        args: list = []
        if workspace is not None:
            sql += " WHERE COALESCE(w.key,'studypal')=?"; args.append(workspace)
        sql += " ORDER BY c.sort_order,c.id"
        rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def create_course(name: str, *, teacher: str = "", nature: str = "选修课",
                  credits: float = 0, note: str = "", workspace: str = 'studypal') -> dict:
    """创建一个空课程容器，后续知识点和材料都挂到这门课。"""
    course_name = (name or "").strip()
    if not course_name:
        raise ValueError("课程名称不能为空")
    if len(course_name) > 80:
        raise ValueError("课程名称不能超过 80 个字符")
    conn = connect()
    try:
        exists = conn.execute("SELECT id FROM courses WHERE name = ?", (course_name,)).fetchone()
        if exists:
            raise ValueError("课程已存在")
        sort_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_order FROM courses"
        ).fetchone()["next_order"]
        workspace_id = conn.execute("SELECT id FROM workspaces WHERE key=?", (workspace,)).fetchone()
        if not workspace_id:
            raise ValueError('工作区不存在')
        cur = conn.execute(
            """INSERT INTO courses (name, teacher, nature, credits, note, status, sort_order, workspace_id)
               VALUES (?, ?, ?, ?, ?, '在读', ?, ?)""",
            (course_name, (teacher or "").strip(), (nature or "选修课").strip(),
             max(0, float(credits or 0)), (note or "").strip(), sort_order, workspace_id['id']),
        )
        conn.commit()
        row = conn.execute(
            """SELECT c.*, w.key AS workspace_key, w.name AS workspace_name,
                      (SELECT COUNT(*) FROM kp WHERE course_id=c.id
                         AND COALESCE(deleted_at,'')='') AS kp_count
                 FROM courses c LEFT JOIN workspaces w ON w.id=c.workspace_id WHERE c.id = ?""",
            (cur.lastrowid,),
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def list_kp(course: str | None = None, band: str | None = None,
            mastery: str | None = None, freq: str | None = None,
            workspace: str | None = None) -> list[dict]:
    # T2.21: soft-deleted nodes are hidden from every kp read path.
    sql = """SELECT kp.*, co.name AS course_name FROM kp
             JOIN courses co ON co.id = kp.course_id WHERE COALESCE(kp.deleted_at,'')=''"""
    args: list = []
    if workspace is not None:
        sql += " AND COALESCE((SELECT key FROM workspaces WHERE id=co.workspace_id),'studypal') = ?"
        args.append(workspace)
    if course:
        sql += " AND co.name = ?"
        args.append(course)
    if band:
        sql += " AND kp.band = ?"
        args.append(band)
    if mastery:
        sql += " AND kp.mastery = ?"
        args.append(mastery)
    if freq:
        sql += " AND kp.freq = ?"
        args.append(freq)
    sql += " ORDER BY co.sort_order, kp.band, kp.id"
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def get_kp(kp_id: int) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            """SELECT kp.*, co.name AS course_name FROM kp
               JOIN courses co ON co.id = kp.course_id
              WHERE kp.id = ? AND COALESCE(kp.deleted_at,'')=''""",
            (kp_id,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["pre"] = [dict(r) for r in conn.execute(
            """SELECT kp2.id, kp2.name FROM kp_edges e JOIN kp kp2 ON kp2.id = e.from_kp
               WHERE e.to_kp = ? AND e.kind = 'pre' AND kp2.course_id=?
                 AND COALESCE(kp2.deleted_at,'')=''""", (kp_id, row['course_id'])).fetchall()]
        d["related"] = [dict(r) for r in conn.execute(
            """SELECT kp2.id, kp2.name FROM kp_edges e JOIN kp kp2 ON kp2.id = e.from_kp
               WHERE e.to_kp = ? AND e.kind = 'related' AND kp2.course_id=?
                 AND COALESCE(kp2.deleted_at,'')=''""", (kp_id, row['course_id'])).fetchall()]
        d["quiz"] = [dict(r) for r in conn.execute(
            "SELECT id, stem, answer, analysis, difficulty FROM quiz WHERE kp_id = ?", (kp_id,)).fetchall()]
        meta = conn.execute('SELECT * FROM knowledge_node_meta WHERE kp_id=?', (kp_id,)).fetchone()
        d['meta'] = dict(meta) if meta else {}
        d['attributes'] = json.loads(meta['attributes_json']) if meta and meta['attributes_json'] else {}
        d['aliases'] = [row['alias'] for row in conn.execute(
            'SELECT alias FROM kp_aliases WHERE kp_id=? ORDER BY id', (kp_id,)
        ).fetchall()]
        # P2-3: evidence a rescan retired is history, not current support.
        d['evidence'] = [dict(r) for r in conn.execute(
            '''SELECT e.id,e.content,e.locator_type,e.locator_start,e.locator_end,
                      sf.path,sf.source_kind
                 FROM kp_evidence ke JOIN evidence e ON e.id=ke.evidence_id
                 JOIN source_files sf ON sf.id=e.source_id
                WHERE ke.kp_id=? AND COALESCE(e.stale,0)=0 ORDER BY e.id''', (kp_id,)
        ).fetchall()]
        d['relations'] = [dict(r) for r in conn.execute(
            '''SELECT e.id,e.kind,e.from_kp,e.to_kp,
                      CASE WHEN e.from_kp=? THEN 'outgoing' ELSE 'incoming' END AS direction,
                      CASE WHEN e.from_kp=? THEN e.to_kp ELSE e.from_kp END AS other_kp,
                      other.name AS other_name, rm.reason,rm.confidence,rm.evidence_id
                 FROM kp_edges e
                 JOIN kp a ON a.id=e.from_kp JOIN kp b ON b.id=e.to_kp
                 JOIN kp other ON other.id=CASE WHEN e.from_kp=? THEN e.to_kp ELSE e.from_kp END
                 LEFT JOIN knowledge_relation_meta rm ON rm.edge_id=e.id
                WHERE (e.from_kp=? OR e.to_kp=?)
                  AND a.course_id=? AND b.course_id=?
                  AND COALESCE(a.deleted_at,'')='' AND COALESCE(b.deleted_at,'')=''
                ORDER BY e.kind,other.name''',
            (kp_id, kp_id, kp_id, kp_id, kp_id, row['course_id'], row['course_id'])
        ).fetchall()]
        d['revisions'] = [dict(r) for r in conn.execute(
            '''SELECT id,field,old_value,new_value,origin,confirmed,created_at
                 FROM revisions WHERE kp_id=? ORDER BY id DESC LIMIT 50''', (kp_id,)
        ).fetchall()]
        return d
    finally:
        conn.close()


def require_live_kp(kp_id: int) -> dict:
    """Return a kp that exists and is not soft deleted (tag attach/detach guard).

    O-KB-5: tagging a kp must reject deleted nodes instead of silently linking
    them; the tag layer reuses this helper so kp validation stays in one place.
    """
    conn = connect()
    try:
        row = conn.execute(
            """SELECT kp.id, kp.name, kp.course_id, co.workspace_id
                 FROM kp JOIN courses co ON co.id=kp.course_id
                WHERE kp.id=? AND COALESCE(kp.deleted_at,'')=''""",
            (int(kp_id),)).fetchone()
    finally:
        conn.close()
    if not row:
        raise ValueError('知识点不存在或已删除')
    return dict(row)


def kp_neighbors(kp_id: int, *, depth: int = 1, kind: str | None = None) -> dict:
    """Return a bounded, directional graph traversal for a knowledge card."""
    depth = max(1, min(int(depth), 3))
    conn = connect()
    try:
        root = conn.execute(
            """SELECT kp.id,kp.course_id,kp.name,kp.chapter,kp.chapter_key,kp.section_key,
                      kp.kp_type,kp.mastery,kp.gist,kp.source,co.name AS course_name,
                      COALESCE(w.key,'studypal') AS workspace_key
                 FROM kp JOIN courses co ON co.id=kp.course_id
                 LEFT JOIN workspaces w ON w.id=co.workspace_id
                WHERE kp.id=? AND COALESCE(kp.deleted_at,'')=''""", (kp_id,)
        ).fetchone()
        if not root:
            raise ValueError('知识点不存在')
        edges = [dict(row) for row in conn.execute(
            '''SELECT e.id,e.from_kp,e.to_kp,e.kind,rm.reason,rm.confidence,rm.evidence_id
                 FROM kp_edges e LEFT JOIN knowledge_relation_meta rm ON rm.edge_id=e.id
                 JOIN kp a ON a.id=e.from_kp JOIN courses ca ON ca.id=a.course_id
                 LEFT JOIN workspaces wa ON wa.id=ca.workspace_id
                 JOIN kp b ON b.id=e.to_kp JOIN courses cb ON cb.id=b.course_id
                 LEFT JOIN workspaces wb ON wb.id=cb.workspace_id
                WHERE COALESCE(a.deleted_at,'')='' AND COALESCE(b.deleted_at,'')=''
                  AND COALESCE(wa.key,'studypal')=?
                  AND COALESCE(wb.key,'studypal')=?
                  AND a.course_id=? AND b.course_id=?''',
            (root['workspace_key'], root['workspace_key'], root['course_id'], root['course_id'])
        ).fetchall()]
        if kind:
            edges = [edge for edge in edges if edge['kind'] == kind]
        adjacency: dict[int, list[tuple[int, dict, str]]] = {}
        for edge in edges:
            adjacency.setdefault(edge['from_kp'], []).append((edge['to_kp'], edge, 'outgoing'))
            adjacency.setdefault(edge['to_kp'], []).append((edge['from_kp'], edge, 'incoming'))
        visited = {kp_id}
        queue: list[tuple[int, int, list[int]]] = [(kp_id, 0, [kp_id])]
        found: dict[int, dict] = {}
        paths: list[dict] = []
        while queue:
            current, current_depth, path = queue.pop(0)
            if current_depth >= depth:
                continue
            for other_id, edge, direction in adjacency.get(current, []):
                next_path = path + [other_id]
                paths.append({'edge_id': edge['id'], 'kind': edge['kind'], 'direction': direction,
                              'from_kp': edge['from_kp'], 'to_kp': edge['to_kp'],
                              'depth': current_depth + 1, 'path': next_path,
                              'reason': edge.get('reason') or '', 'confidence': edge.get('confidence')})
                if other_id in visited:
                    continue
                visited.add(other_id)
                queue.append((other_id, current_depth + 1, next_path))
                found[other_id] = {'depth': current_depth + 1, 'path': next_path}
        ids = list(found)
        nodes = []
        if ids:
            placeholders = ','.join('?' for _ in ids)
            nodes = [dict(row) for row in conn.execute(
                f'''SELECT kp.id,kp.name,kp.chapter,kp.chapter_key,kp.section_key,kp.band,
                           kp.kp_type,kp.mastery,kp.gist,kp.source,co.name AS course_name
                      FROM kp JOIN courses co ON co.id=kp.course_id
                     WHERE kp.id IN ({placeholders})
                       AND COALESCE(kp.deleted_at,'')=''
                     ORDER BY kp.chapter_key,kp.id''', ids
            ).fetchall()]
            for node in nodes:
                node.update(found[node['id']])
        return {'root': dict(root), 'depth': depth, 'kind': kind or 'all',
                'nodes': nodes, 'paths': paths}
    finally:
        conn.close()


def kp_evidence(kp_id: int) -> list[dict]:
    detail = get_kp(kp_id)
    if not detail:
        raise ValueError('知识点不存在')
    return detail.get('evidence', [])


def kp_revisions(kp_id: int) -> list[dict]:
    detail = get_kp(kp_id)
    if not detail:
        raise ValueError('知识点不存在')
    return detail.get('revisions', [])


KP_EDITABLE_FIELDS = {
    "name", "band", "chapter", "gist", "detail", "trap", "source",
    "related", "kp_type", "freq",
}

# Editing these fields can change derived navigation/structure, so the write
# event refreshes the materialized views instead of a later GET doing it.
KP_STRUCTURE_FIELDS = {"name", "chapter", "source"}


def update_kp(kp_id: int, values: dict[str, str], origin: str = "manual") -> dict:
    """Update editable knowledge fields and leave a revision for every change."""
    patch = {key: str(value or "").strip() for key, value in values.items()
             if key in KP_EDITABLE_FIELDS}
    if not patch:
        raise ValueError("没有可修改的知识点字段")
    if "name" in patch and not patch["name"]:
        raise ValueError("知识点名称不能为空")
    limits = {
        "name": 180, "band": 80, "chapter": 180, "gist": 1000,
        "detail": 4000, "trap": 1000, "source": 500, "related": 500,
        "kp_type": 40, "freq": 20,
    }
    for field, value in patch.items():
        if len(value) > limits[field]:
            raise ValueError(f"{field} 内容过长")

    conn = connect()
    try:
        row = conn.execute(
            """SELECT kp.*, co.name AS course_name FROM kp
                 JOIN courses co ON co.id=kp.course_id WHERE kp.id=?""", (kp_id,)).fetchone()
        if not row:
            raise ValueError("知识点不存在")
        if str(row["deleted_at"] or "").strip():
            raise ValueError("知识点已删除，不能编辑")
        if "name" in patch:
            duplicate = conn.execute(
                "SELECT 1 FROM kp WHERE course_id=? AND name=? AND id<>?",
                (row["course_id"], patch["name"], kp_id),
            ).fetchone()
            if duplicate:
                raise ValueError("同一课程中已有同名知识点")
        old_name = str(row["name"] or "")
        new_key = None
        if "name" in patch and patch["name"] != old_name:
            new_key = _node_key(row["course_id"], patch["name"])
            if _node_key_conflict(conn, row["course_id"], new_key, kp_id) is not None:
                raise ValueError("同一课程中已有同名知识点")
        changed = []
        for field, new_value in patch.items():
            old_value = str(row[field] or "")
            if old_value == new_value:
                continue
            if field == 'name':
                conn.execute('INSERT OR IGNORE INTO kp_aliases(kp_id,alias,origin) VALUES(?,?,?)',
                             (kp_id, old_value, origin))
            conn.execute(
                f"UPDATE kp SET {field}=?, updated_at=datetime('now','localtime') WHERE id=?",
                (new_value, kp_id),
            )
            conn.execute(
                """INSERT INTO revisions (kp_id, field, old_value, new_value, origin, confirmed)
                   VALUES (?,?,?,?,?,1)""",
                (kp_id, field, old_value, new_value, origin),
            )
            changed.append(field)
        if new_key is not None:
            _retarget_identity_links(conn, kp_id, row["course_name"], old_name,
                                     patch["name"], new_key)
        conn.commit()
        result = conn.execute(
            "SELECT kp.*, co.name AS course_name FROM kp JOIN courses co ON co.id=kp.course_id WHERE kp.id=?",
            (kp_id,),
        ).fetchone()
    finally:
        conn.close()
    if changed and KP_STRUCTURE_FIELDS & set(changed):
        # A knowledge edit is a write event: refresh derived graph state now so
        # GET /api/graph stays a pure read.
        try:
            sync_memory_views(result["course_name"])
        except Exception:
            logger.exception('graph view sync after knowledge edit failed')
    return {"ok": True, "changed": changed, "kp": dict(result)}


def update_node_contract(kp_id: int, values: dict, origin: str = 'manual-graph') -> dict:
    """Persist the structured part of a knowledge node contract."""
    conn = connect()
    try:
        row = conn.execute('''SELECT kp.id,kp.course_id,co.name AS course_name
                              FROM kp JOIN courses co ON co.id=kp.course_id WHERE kp.id=?''', (kp_id,)).fetchone()
        if not row:
            raise ValueError('知识点不存在')
        profile = domain_profiles.profile_for_course(row['course_name'], values.get('workspace', ''))
        domain = str(values.get('domain') or profile['id']).strip()
        if domain not in domain_profiles.load_profiles():
            raise ValueError('未注册的领域档案')
        attributes = values.get('attributes', {})
        aliases = values.get('aliases', [])
        evidence_ids = values.get('evidence_ids', [])
        if not isinstance(attributes, dict) or not isinstance(aliases, list) or not isinstance(evidence_ids, list):
            raise ValueError('领域属性、别名和证据必须是数组或对象')
        evidence_ids = [int(item) for item in evidence_ids if str(item).isdigit()]
        valid_evidence = {item['id'] for item in conn.execute(
            '''SELECT e.id FROM evidence e JOIN source_files sf ON sf.id=e.source_id
               WHERE sf.course_id=? AND COALESCE(e.stale,0)=0''', (row['course_id'],)).fetchall()}
        invalid_evidence = sorted(set(evidence_ids) - valid_evidence)
        if invalid_evidence:
            raise ValueError(
                '证据必须来自当前课程的原始材料且未被重扫淘汰；无效证据：'
                + ','.join(str(item) for item in invalid_evidence[:10]))
        payload = {
            'schema_version': 'cyberpal.knowledge.v1', 'domain': domain,
            'attributes_json': json.dumps(attributes, ensure_ascii=False, sort_keys=True),
            'source_refs_json': json.dumps([{'evidence_id': evidence_id} for evidence_id in evidence_ids], ensure_ascii=False),
        }
        with conn:
            conn.execute('''INSERT INTO knowledge_node_meta(kp_id,schema_version,domain,attributes_json,source_refs_json,status)
                            VALUES(?,?,?,?,?,'confirmed')
                            ON CONFLICT(kp_id) DO UPDATE SET domain=excluded.domain,
                            attributes_json=excluded.attributes_json,source_refs_json=excluded.source_refs_json,
                            updated_at=datetime('now','localtime')''',
                         (kp_id, payload['schema_version'], payload['domain'], payload['attributes_json'], payload['source_refs_json']))
            conn.execute('DELETE FROM kp_evidence WHERE kp_id=?', (kp_id,))
            conn.executemany('INSERT INTO kp_evidence(kp_id,evidence_id,role) VALUES(?,?,?)',
                             [(kp_id, evidence_id, 'supports') for evidence_id in evidence_ids])
            conn.execute('DELETE FROM kp_aliases WHERE kp_id=?', (kp_id,))
            conn.executemany('INSERT OR IGNORE INTO kp_aliases(kp_id,alias,origin) VALUES(?,?,?)',
                             [(kp_id, str(alias).strip()[:180], origin) for alias in aliases if str(alias).strip()])
        return {'ok': True, 'kp_id': kp_id, 'domain': domain, 'attributes': attributes,
                'aliases': [str(alias).strip() for alias in aliases if str(alias).strip()],
                'evidence_ids': evidence_ids}
    finally:
        conn.close()


def _node_key(course_id: int, name: str) -> str:
    normalized = ' '.join(str(name or '').split()).casefold()
    return 'kp_' + hashlib.sha256(f'cyberpal:{course_id}:{normalized}'.encode('utf-8')).hexdigest()[:24]


def _node_key_conflict(conn, course_id: int, node_key: str, kp_id: int) -> int | None:
    """Return another kp that would own this stable key after a rename."""
    row = conn.execute(
        'SELECT kp_id FROM kp_identities WHERE node_key=? AND kp_id<>?',
        (node_key, kp_id)).fetchone()
    if row is not None:
        return row['kp_id']
    # Identities are lazy; compare the deterministic key of names that have not
    # been synced yet so a rename cannot create a future UNIQUE collision.
    for other in conn.execute(
            'SELECT id,name FROM kp WHERE course_id=? AND id<>?', (course_id, kp_id)):
        if _node_key(course_id, other['name']) == node_key:
            return other['id']
    return None


def _retarget_identity_links(conn, kp_id: int, course_name: str, old_name: str,
                             new_name: str, new_key: str) -> None:
    """Move a renamed node's identity and bridge references in one transaction.

    The old name stays in kp_aliases/revisions as history, but kp_identities
    must not keep a stale key. Legacy bridges match by course/name text, so
    they are retargeted too; otherwise the next sync_bridge_links() would
    immediately mark the relation unresolved again.
    """
    updated = conn.execute(
        """UPDATE kp_identities
              SET node_key=?, updated_at=datetime('now','localtime')
            WHERE kp_id=?""", (new_key, kp_id))
    if updated.rowcount == 0:
        conn.execute('INSERT INTO kp_identities(kp_id,node_key) VALUES(?,?)',
                     (kp_id, new_key))
    conn.execute('UPDATE bridges SET kp_a=? WHERE course_a=? AND kp_a=?',
                 (new_name, course_name, old_name))
    conn.execute('UPDATE bridges SET kp_b=? WHERE course_b=? AND kp_b=?',
                 (new_name, course_name, old_name))
    conn.execute(
        """UPDATE bridge_links
              SET a_node_key=?, updated_at=datetime('now','localtime')
            WHERE a_kp_id=?""", (new_key, kp_id))
    conn.execute(
        """UPDATE bridge_links
              SET b_node_key=?, updated_at=datetime('now','localtime')
            WHERE b_kp_id=?""", (new_key, kp_id))


def ensure_node_identity(kp_id: int) -> dict:
    conn = connect()
    try:
        row = conn.execute('SELECT id,course_id,name FROM kp WHERE id=?', (kp_id,)).fetchone()
        if not row:
            raise ValueError('知识点不存在')
        identity = conn.execute('SELECT kp_id,node_key FROM kp_identities WHERE kp_id=?', (kp_id,)).fetchone()
        if not identity:
            key = _node_key(row['course_id'], row['name'])
            conn.execute('INSERT OR IGNORE INTO kp_identities(kp_id,node_key) VALUES(?,?)', (kp_id, key))
            conn.commit()
            identity = conn.execute('SELECT kp_id,node_key FROM kp_identities WHERE kp_id=?', (kp_id,)).fetchone()
        return dict(identity)
    finally:
        conn.close()


def sync_node_identities() -> int:
    conn = connect()
    try:
        rows = conn.execute('SELECT id,course_id,name FROM kp').fetchall()
        conn.executemany('INSERT OR IGNORE INTO kp_identities(kp_id,node_key) VALUES(?,?)',
                         [(row['id'], _node_key(row['course_id'], row['name'])) for row in rows])
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def create_edge(from_kp: int, to_kp: int, kind: str, *, evidence_id: int | None = None,
                reason: str = '', confidence: float = 0.5) -> dict:
    if kind not in {'pre', 'related', 'part_of', 'supports', 'example_of', 'causes', 'contrasts'}:
        raise ValueError("当前课程图谱关系只能是 pre / related / part_of / supports / example_of / causes / contrasts")
    if from_kp == to_kp:
        raise ValueError("知识点不能连接自己")
    conn = connect()
    try:
        nodes = conn.execute(
            "SELECT id, course_id, COALESCE(deleted_at,'') AS deleted_at FROM kp WHERE id IN (?,?)",
            (from_kp, to_kp)
        ).fetchall()
        if len(nodes) != 2 or len({row["course_id"] for row in nodes}) != 1:
            raise ValueError("只能连接同一课程内的知识点")
        if any(row["deleted_at"] for row in nodes):
            raise ValueError("知识点已删除，不能建立关系")
        conn.execute(
            "INSERT OR IGNORE INTO kp_edges (from_kp,to_kp,kind) VALUES (?,?,?)",
            (from_kp, to_kp, kind),
        )
        edge = conn.execute(
            "SELECT id, from_kp, to_kp, kind FROM kp_edges WHERE from_kp=? AND to_kp=? AND kind=?",
            (from_kp, to_kp, kind),
        ).fetchone()
        safe_confidence = max(0.0, min(1.0, float(confidence or 0.5)))
        conn.execute('''INSERT INTO knowledge_relation_meta(edge_id,evidence_id,reason,confidence)
                        VALUES(?,?,?,?)
                        ON CONFLICT(edge_id) DO UPDATE SET evidence_id=excluded.evidence_id,
                        reason=excluded.reason,confidence=excluded.confidence,
                        updated_at=datetime('now','localtime')''',
                     (edge['id'], evidence_id, str(reason or '')[:1000], safe_confidence))
        conn.commit()
        return {"ok": True, "edge": dict(edge)}
    finally:
        conn.close()


def delete_edge(edge_id: int) -> dict:
    conn = connect()
    try:
        cur = conn.execute("DELETE FROM kp_edges WHERE id=?", (edge_id,))
        conn.commit()
        return {"ok": True, "deleted": cur.rowcount > 0}
    finally:
        conn.close()


def review_relation_candidate(candidate_id: int, action: str, origin: str = 'manual') -> dict:
    if action not in {'confirmed', 'rejected'}:
        raise ValueError('关系候选审核动作只能是 confirmed 或 rejected')
    conn = connect()
    try:
        row = conn.execute('SELECT * FROM relation_candidates WHERE id=?', (candidate_id,)).fetchone()
        if not row:
            raise ValueError('关系候选不存在')
        if row['status'] != 'pending':
            raise ValueError('该关系候选已经审核')
        edge_id = None
        if action == 'confirmed':
            if not row['from_kp'] or not row['to_kp']:
                raise ValueError('关系候选缺少正式节点，不能确认')
            edge = create_edge(row['from_kp'], row['to_kp'], row['kind'], evidence_id=row['evidence_id'],
                               reason=row['reason'], confidence=row['confidence'])['edge']
            edge_id = edge['id']
        with conn:
            conn.execute('''UPDATE relation_candidates SET status=?,target_edge_id=?,reviewed_at=datetime('now','localtime')
                            WHERE id=?''', (action, edge_id, candidate_id))
        return {'ok': True, 'status': action, 'target_edge_id': edge_id}
    finally:
        conn.close()


def list_relation_candidates(course: str | None = None, status: str = 'pending') -> list[dict]:
    sql = '''SELECT rc.*, ca.name AS from_course, cb.name AS to_course,
                    ev.locator_type AS evidence_locator_type,
                    ev.locator_start AS evidence_locator_start,
                    ev.locator_end AS evidence_locator_end,
                    ev.content AS evidence_content
             FROM relation_candidates rc
             LEFT JOIN kp ka ON ka.id=rc.from_kp LEFT JOIN courses ca ON ca.id=ka.course_id
             LEFT JOIN kp kb ON kb.id=rc.to_kp LEFT JOIN courses cb ON cb.id=kb.course_id
             LEFT JOIN evidence ev ON ev.id=rc.evidence_id AND COALESCE(ev.stale,0)=0
             WHERE rc.status=? AND (rc.evidence_id IS NULL OR ev.id IS NOT NULL)'''
    args: list = [status]
    if course:
        sql += ' AND (ca.name=? OR cb.name=?)'
        args.extend([course, course])
    sql += ' ORDER BY rc.id'
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def write_mastery(kp_id: int, new_value: str, origin: str = "chat", confirmed: bool = True) -> dict:
    """写回掌握度。留痕是硬要求——记忆改了必须可追溯。"""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT mastery, name, COALESCE(deleted_at,'') AS deleted_at FROM kp WHERE id = ?",
            (kp_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "知识点不存在"}
        if row["deleted_at"]:
            return {"ok": False, "error": "知识点已删除，不能写回掌握度"}
        old = row["mastery"]
        if old == new_value:
            return {"ok": True, "changed": False, "kp": row["name"], "mastery": old}
        conn.execute(
            "UPDATE kp SET mastery = ?, updated_at = datetime('now','localtime') WHERE id = ?",
            (new_value, kp_id),
        )
        conn.execute(
            """INSERT INTO revisions (kp_id, field, old_value, new_value, origin, confirmed)
               VALUES (?,?,?,?,?,?)""",
            (kp_id, "mastery", old, new_value, origin, 1 if confirmed else 0),
        )
        conn.commit()
        return {"ok": True, "changed": True, "kp": row["name"], "from": old, "to": new_value}
    finally:
        conn.close()


def mastery_matrix(course: str) -> dict:
    """掌握度矩阵：行是章节，列是四档。"""
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT kp.chapter,
                      SUM(CASE WHEN kp.mastery='熟练' THEN 1 ELSE 0 END) AS m1,
                      SUM(CASE WHEN kp.mastery='会用' THEN 1 ELSE 0 END) AS m2,
                      SUM(CASE WHEN kp.mastery='读过' THEN 1 ELSE 0 END) AS m3,
                      SUM(CASE WHEN kp.mastery='未读' THEN 1 ELSE 0 END) AS m4,
                      SUM(CASE WHEN kp.freq='高频' AND kp.mastery!='熟练' THEN 1 ELSE 0 END) AS risk,
                      COUNT(*) AS total
               FROM kp JOIN courses co ON co.id = kp.course_id
               WHERE co.name = ? AND COALESCE(kp.deleted_at,'')=''
               GROUP BY kp.chapter ORDER BY kp.chapter""",
            (course,),
        ).fetchall()
        return {"course": course, "rows": [dict(r) for r in rows]}
    finally:
        conn.close()


def ensure_chapter_catalog(course: str) -> list[dict]:
    """Materialize the explicitly selected (active) catalog, without making headings KPs.

    O-DATA-5: a candidate/historical catalog upload is user intent data, not
    navigation. Only select_catalog_source() promotes one source to active and
    only that source may drive chapter navigation.
    """
    conn = connect()
    try:
        course_row = conn.execute('SELECT id FROM courses WHERE name=?', (course,)).fetchone()
        if not course_row:
            return []
        cid = course_row['id']
        source = conn.execute('''SELECT id,path,sha256 FROM source_files
                                 WHERE course_id=? AND status='indexed'
                                   AND source_kind='course_catalog'
                                   AND catalog_status='active'
                                 ORDER BY id
                                 LIMIT 1''', (cid,)).fetchone()
        if not source:
            return []
        existing = conn.execute('''SELECT COUNT(*) AS count, MAX(source_sha256) AS source_sha256,
                                         MAX(catalog_version) AS catalog_version
                                   FROM chapter_catalog WHERE course_id=?''', (cid,)).fetchone()
        if (existing['count'] and existing['source_sha256'] == source['sha256']
                          and existing['catalog_version'] == chapter_catalog.CATALOG_VERSION):
            return _chapter_catalog_rows(conn, cid)
        try:
            from . import sources
            _, _, evidence = sources.extract_structured(Path(source['path']))
            chapters = chapter_catalog.extract_catalog(evidence)
        except Exception:
            chapters = []
        if chapters:
            with conn:
                conn.execute('DELETE FROM chapter_catalog WHERE course_id=?', (cid,))
                for item in chapters:
                    cur = conn.execute('''INSERT INTO chapter_catalog
                        (course_id,source_id,chapter_key,number,title,start_locator,end_locator,source_sha256,catalog_version)
                        VALUES(?,?,?,?,?,?,?,?,?)''',
                        (cid, source['id'], item['key'], item['number'], item['title'],
                         item['start_locator'], item['end_locator'], source['sha256'],
                         item.get('catalog_version', chapter_catalog.CATALOG_VERSION)))
                    for section in item['sections']:
                        conn.execute('''INSERT INTO chapter_sections
                            (chapter_id,section_key,number,title,start_locator,end_locator)
                            VALUES(?,?,?,?,?,?)''',
                            (cur.lastrowid, section['key'], section['number'], section['title'],
                             section['start_locator'], section['end_locator']))
        return _chapter_catalog_rows(conn, cid)
    finally:
        conn.close()


def catalog_sources(course: str) -> list[dict]:
    """Return uploaded catalog candidates without exposing private file internals."""
    conn = connect()
    try:
        row = conn.execute('SELECT id FROM courses WHERE name=?', (course,)).fetchone()
        if not row:
            return []
        return [dict(item) for item in conn.execute(
            '''SELECT sf.id,sf.path,sf.status,sf.sha256,sf.source_kind,sf.source_role,sf.catalog_status,
                      (SELECT COUNT(*) FROM chapter_catalog cc WHERE cc.source_id=sf.id) AS chapter_count,
                      (SELECT COUNT(*) FROM chapter_catalog cc JOIN chapter_sections cs ON cs.chapter_id=cc.id
                       WHERE cc.source_id=sf.id) AS section_count
               FROM source_files sf WHERE sf.course_id=? AND sf.source_kind='course_catalog'
               ORDER BY CASE WHEN sf.catalog_status='active' THEN 0 ELSE 1 END,sf.id DESC''',
            (row['id'],),
        ).fetchall()]
    finally:
        conn.close()


def select_catalog_source(course: str, source_id: int) -> dict:
    """Make one indexed course-catalog upload authoritative for navigation."""
    conn = connect()
    try:
        course_row = conn.execute('SELECT id FROM courses WHERE name=?', (course,)).fetchone()
        if not course_row:
            raise ValueError('课程不存在')
        source = conn.execute(
            '''SELECT id,path,sha256,status,source_kind FROM source_files
               WHERE id=? AND course_id=?''', (source_id, course_row['id'])
        ).fetchone()
        if not source or source['source_kind'] != 'course_catalog':
            raise ValueError('只能把课程目录材料设为当前目录')
        if source['status'] != 'indexed':
            raise ValueError('这份课程目录尚未完成识别，暂不能设为当前目录')
        with conn:
            conn.execute("UPDATE source_files SET catalog_status='historical' WHERE course_id=? AND source_kind='course_catalog'", (course_row['id'],))
            conn.execute("UPDATE source_files SET catalog_status='active' WHERE id=?", (source_id,))
        # Re-materialization keeps existing kp/kp_edges rows; only the navigation
        # outline and section anchors are regenerated from the selected source.
    finally:
        conn.close()
    # Explicit catalog selection is a write event: materialize the derived
    # outline and node keys here, not on the next read request.
    sync_memory_views(course, materialize_catalog=True)
    conn = connect()
    try:
        course_row = conn.execute('SELECT id FROM courses WHERE name=?', (course,)).fetchone()
        chapters = _chapter_catalog_rows(conn, course_row['id']) if course_row else []
    finally:
        conn.close()
    return {
        'ok': True, 'course': course, 'source_id': source_id,
        'status': 'active', 'chapters': chapters,
    }


def _chapter_catalog_rows(conn, course_id: int) -> list[dict]:
    chapters = [dict(row) for row in conn.execute(
        '''SELECT id,chapter_key AS key,number,title,start_locator,end_locator,status
           FROM chapter_catalog WHERE course_id=? AND status='active' ORDER BY number,id''',
        (course_id,),
    ).fetchall()]
    for chapter in chapters:
        chapter['sections'] = [dict(row) for row in conn.execute(
            '''SELECT id,section_key AS key,number,title,start_locator,end_locator,status
               FROM chapter_sections WHERE chapter_id=? AND status='active' ORDER BY number,id''',
            (chapter['id'],),
        ).fetchall()]
    return chapters


def _node_structure(node: dict, chapters: list[dict]) -> tuple[str, str]:
    """Resolve legacy chapter text to stable keys conservatively."""
    number = chapter_catalog.chapter_number(str(node.get('chapter') or ''))
    source = str(node.get('source') or '')
    if number is None:
        match = re.search(r'(?:教材\s*|第\s*)(\d{1,2})(?:\.\d+)?\s*(?:章)?', source)
        number = int(match.group(1)) if match else None
    if number is None:
        return '', ''
    key = chapter_catalog.chapter_key(number)
    valid = {item['key'] for item in chapters}
    if valid and key not in valid:
        return '', ''
    section = ''
    section_match = re.search(rf'(?<!\d){number}\.(\d{{1,2}})', f"{node.get('chapter','')} {source}")
    if section_match:
        section = chapter_catalog.section_key(number, f'{number}.{section_match.group(1)}')
    return key, section


def _node_structure_rows(conn, course_id: int, chapters: list[dict]):
    """Resolve stable chapter/section keys for graph nodes without writing.

    Returns (effective, updates): effective maps node id to the (chapter_key,
    section_key) pair a materialized sync would store; updates lists only the
    rows whose stored keys differ.
    """
    rows = conn.execute('''SELECT kp.id,kp.chapter,kp.chapter_key,kp.section_key,kp.source,kp.name,kp.kp_type,
                                  COALESCE(
                                      (SELECT sf.source_kind
                                         FROM kp_evidence ke
                                         JOIN evidence e ON e.id=ke.evidence_id
                                         JOIN source_files sf ON sf.id=e.source_id
                                        WHERE ke.kp_id=kp.id
                                        ORDER BY ke.evidence_id LIMIT 1),
                                      (SELECT sf.source_kind FROM source_files sf
                                        WHERE sf.course_id=kp.course_id AND sf.path=kp.source LIMIT 1),
                                      ''
                                  ) AS source_kind
                           FROM kp WHERE kp.course_id=?''', (course_id,)).fetchall()
    valid_sections = {
        section['key']
        for chapter in chapters
        for section in chapter.get('sections', [])
    }
    chapter_keys = {item['key'] for item in chapters}
    section_ranges = {
        section['key']: section for chapter in chapters for section in chapter.get('sections', [])
    }
    source_by_chapter = {
        row['chapter_key']: row['source_id']
        for row in conn.execute(
            'SELECT chapter_key,source_id FROM chapter_catalog WHERE course_id=?', (course_id,)
        ).fetchall()
    }
    effective: dict[int, tuple[str, str]] = {}
    updates: list[tuple[str, str, int]] = []
    for row in rows:
        node = dict(row)
        if chapter_catalog.is_structural_node(node, row['source_kind']) or row['kp_type'] == 'section':
            continue
        key, section = _node_structure(node, chapters)
        # WorkPal/LifePal materials may be Wiki topics rather than textbooks.
        # Keep their explicit topic as a stable chapter-like cluster so the
        # graph remains navigable without inventing a course catalog.
        if not key and not chapters and str(node.get('chapter') or '').strip():
            topic = re.sub(r'\s+', '-', str(node['chapter']).strip().lower())
            topic = re.sub(r'[^a-z0-9\u4e00-\u9fff_-]+', '-', topic).strip('-_') or 'unassigned'
            key = f'topic-{topic[:72]}'
        if row['chapter_key'] in chapter_keys:
            key = row['chapter_key']
        if row['section_key'] in valid_sections:
            section = row['section_key']
        if not key:
            continue
        if section not in valid_sections:
            section = ''
        # When a legacy node has no section in its source label, use its
        # evidence locator to place it in the deepest matching textbook range.
        if not section and source_by_chapter.get(key):
            evidence_rows = conn.execute(
                '''SELECT e.locator_start FROM kp_evidence ke JOIN evidence e ON e.id=ke.evidence_id
                   WHERE ke.kp_id=? ORDER BY e.locator_start''', (row['id'],)
            ).fetchall()
            if not evidence_rows and row['name']:
                chapter = next((item for item in chapters if item['key'] == key), None)
                if chapter:
                    evidence_rows = conn.execute(
                        '''SELECT locator_start FROM evidence
                           WHERE source_id=? AND locator_start>=? AND locator_start<=?
                             AND content LIKE ? ORDER BY locator_start LIMIT 8''',
                        (source_by_chapter[key], chapter['start_locator'], chapter['end_locator'],
                         f"%{row['name']}%"),
                    ).fetchall()
            for evidence_row in evidence_rows:
                locator = evidence_row['locator_start']
                matches = [item for item in section_ranges.values()
                           if item['key'].startswith(f'{key}-')
                           and item.get('start_locator') is not None
                           and item['start_locator'] <= locator
                           and (item.get('end_locator') is None or locator <= item['end_locator'])]
                if matches:
                    section = max(matches, key=lambda item: item['number'].count('.'))['key']
                    break
        effective[row['id']] = (key, section)
        if row['chapter_key'] != key or row['section_key'] != section:
            updates.append((key, section, row['id']))
    return effective, updates


def _sync_node_structure(conn, course_id: int, chapters: list[dict]) -> int:
    """Persist the structural resolution; returns the number of changed rows."""
    _, updates = _node_structure_rows(conn, course_id, chapters)
    for key, section, kp_id in updates:
        conn.execute('''UPDATE kp SET chapter_key=?,section_key=?,updated_at=datetime('now','localtime')
                        WHERE id=?''', (key, section, kp_id))
    return len(updates)


def _graph_chapters(conn, course_id: int, nodes: list[dict], *,
                    allow_topic_clusters: bool = True,
                    chapters: list[dict] | None = None) -> list[dict]:
    node_ids = {node['id'] for node in nodes}
    if chapters is None:
        chapters = _chapter_catalog_rows(conn, course_id)
    if not chapters and node_ids:
        if not allow_topic_clusters:
            # A course catalog exists, so navigation must come from an active
            # selection instead of synthesized topic clusters.
            return []
        # A non-course workspace still needs a navigable outline. These
        # clusters come from approved node provenance, not from arbitrary
        # headings guessed by the extractor.
        grouped: dict[tuple[str, str], dict] = {}
        for node in nodes:
            key = str(node.get('chapter_key') or '')
            if not key:
                continue
            group = (key, str(node.get('chapter') or ''))
            entry = grouped.get(group)
            if entry is None:
                entry = {'key': key, 'chapter': group[1], 'first_id': node['id'], 'members': []}
                grouped[group] = entry
            entry['members'].append(node['id'])
        ordered = sorted(grouped.values(), key=lambda item: item['first_id'])
        for index, entry in enumerate(ordered, 1):
            members = sorted(entry['members'])
            chapters.append({
                'id': None, 'key': entry['key'], 'number': index,
                'title': entry['chapter'] or entry['key'], 'start_locator': None,
                'end_locator': None, 'status': 'ready' if members else '待补',
                'sections': [], 'node_ids': members, 'node_count': len(members),
            })
        return chapters
    for chapter in chapters:
        chapter['node_ids'] = sorted(
            node['id'] for node in nodes
            if str(node.get('chapter_key') or '') == chapter['key']
        )
        chapter['node_count'] = len(chapter['node_ids'])
        for section in chapter['sections']:
            section['node_ids'] = sorted(
                node['id'] for node in nodes
                if str(node.get('section_key') or '') == section['key']
            )
            section['node_count'] = len(section['node_ids'])
        chapter['status'] = 'ready' if chapter['node_count'] else '待补'
    return chapters


def _backfill_legacy_node_meta(conn, course_id: int, fallback_domain: str) -> int:
    """Create node metadata rows for legacy nodes as part of a write event."""
    legacy = conn.execute('''SELECT kp.id,kp.source FROM kp
                             WHERE kp.course_id=? AND NOT EXISTS
                             (SELECT 1 FROM knowledge_node_meta nm WHERE nm.kp_id=kp.id)''',
                          (course_id,)).fetchall()
    if not legacy:
        return 0
    conn.executemany('''INSERT OR IGNORE INTO knowledge_node_meta
                        (kp_id,schema_version,domain,source_refs_json,status)
                        VALUES(?,?,?,?, 'confirmed')''', [
        (row['id'], 'cyberpal.knowledge.v1', fallback_domain,
         json.dumps([{'source': row['source'], 'quality': 'legacy'}]
                    if str(row['source'] or '').strip() else [], ensure_ascii=False))
        for row in legacy
    ])
    return len(legacy)


def _purge_inactive_catalog_rows(conn, course_id: int) -> int:
    """Drop navigation materialized from a catalog the user has not selected."""
    return conn.execute(
        """DELETE FROM chapter_catalog
            WHERE course_id=? AND source_id IN (
                SELECT id FROM source_files
                 WHERE course_id=? AND source_kind='course_catalog'
                   AND COALESCE(catalog_status,'none')!='active')""",
        (course_id, course_id)).rowcount


def sync_memory_views(course: str | None = None, *, materialize_catalog: bool = False) -> dict:
    """Materialize derived graph state at explicit write events.

    Read endpoints stay pure, so catalog materialization, legacy node metadata,
    stable identities, node structure and bridge resolution are written here:
    startup, scan completion, catalog selection and knowledge edits call it.
    """
    conn = connect()
    try:
        if course is None:
            courses = [dict(row) for row in conn.execute('SELECT id,name FROM courses ORDER BY id')]
        else:
            courses = [dict(row) for row in conn.execute(
                'SELECT id,name FROM courses WHERE name=?', (course,))]
    finally:
        conn.close()
    node_meta = 0
    structure_updates = 0
    catalog_rows_removed = 0
    for item in courses:
        if materialize_catalog:
            ensure_chapter_catalog(item['name'])
        conn = connect()
        try:
            with conn:
                catalog_rows_removed += _purge_inactive_catalog_rows(conn, item['id'])
            chapters = _chapter_catalog_rows(conn, item['id'])
            fallback_domain = domain_profiles.profile_for_course(item['name'])['id']
            with conn:
                node_meta += _backfill_legacy_node_meta(conn, item['id'], fallback_domain)
                structure_updates += _sync_node_structure(conn, item['id'], chapters)
        finally:
            conn.close()
    identities = sync_node_identities()
    sync_bridge_links()
    return {'courses': len(courses), 'node_meta': node_meta,
            'structure_updates': structure_updates, 'identities': identities,
            'catalog_rows_removed': catalog_rows_removed}


def backfill_section_nodes(course: str) -> dict:
    """Create evidence-backed section anchors and parent relations.

    Anchors make a complete textbook outline visible even when a section has
    not yet produced richer concepts. They are ordinary graph nodes so the
    same focus and traversal rules work for chapters, sections, and concepts.
    """
    ensure_chapter_catalog(course)
    conn = connect()
    created = 0
    relations = 0
    try:
        course_row = conn.execute('SELECT id FROM courses WHERE name=?', (course,)).fetchone()
        if not course_row:
            return {'created': 0, 'relations': 0}
        course_id = course_row['id']
        chapters = conn.execute(
            '''SELECT c.id,c.number,c.title,c.source_id,cs.section_key,cs.number AS section_number,
                      cs.title AS section_title,cs.start_locator,cs.end_locator
               FROM chapter_catalog c JOIN chapter_sections cs ON cs.chapter_id=c.id
               WHERE c.course_id=? AND c.status='active' AND cs.status='active'
               ORDER BY c.number,cs.number''',
            (course_id,),
        ).fetchall()
        with conn:
            for section in chapters:
                chapter_key = f"ch-{int(section['number']):02d}"
                source = f"教材第{section['number']}章 {section['section_number']} · p.{section['start_locator']}"
                anchor = conn.execute(
                    '''SELECT id,section_key FROM kp
                       WHERE course_id=? AND section_key=? AND kp_type='section'
                       ORDER BY id LIMIT 1''',
                    (course_id, section['section_key']),
                ).fetchone()
                if not anchor:
                    anchor = conn.execute(
                        '''SELECT id,name,section_key FROM kp
                           WHERE course_id=? AND chapter_key=? AND kp_type='section'
                             AND source LIKE ?
                           ORDER BY id LIMIT 1''',
                        (course_id, chapter_key,
                         f"教材第{section['number']}章 {section['section_number']} ·%"),
                    ).fetchone()
                if not anchor:
                    # Repair anchors created before section nodes became immutable.
                    # The numbered fallback handles duplicate section titles.
                    anchor = conn.execute(
                        '''SELECT id,name,section_key FROM kp
                           WHERE course_id=? AND chapter_key=? AND kp_type='section'
                             AND name IN (?,?)
                           ORDER BY CASE WHEN name=? THEN 0 ELSE 1 END,id LIMIT 1''',
                        (course_id, chapter_key, section['section_title'],
                         f"{section['section_number']} {section['section_title']}",
                         section['section_title']),
                    ).fetchone()
                if anchor and anchor['section_key'] != section['section_key']:
                    conn.execute(
                        '''UPDATE kp SET section_key=?,updated_at=datetime('now','localtime')
                           WHERE id=?''',
                        (section['section_key'], anchor['id']),
                    )
                evidence = conn.execute(
                    '''SELECT id,content FROM evidence
                       WHERE source_id=? AND locator_start>=? AND locator_start<=?
                         AND COALESCE(stale,0)=0
                       ORDER BY locator_start,id LIMIT 8''',
                    (section['source_id'], section['start_locator'], section['end_locator']),
                ).fetchall()
                evidence_ids = [row['id'] for row in evidence]
                detail = ' '.join(
                    ' '.join(str(item['content'] or '').split())
                    for item in evidence
                )[:800]
                gist = detail[:220] or section['section_title']
                if not anchor:
                    anchor_name = section['section_title']
                    name_taken = conn.execute(
                        'SELECT 1 FROM kp WHERE course_id=? AND name=? LIMIT 1',
                        (course_id, anchor_name),
                    ).fetchone()
                    if name_taken:
                        anchor_name = f"{section['section_number']} {section['section_title']}"
                    cur = conn.execute(
                        '''INSERT INTO kp(course_id,name,band,chapter,chapter_key,section_key,gist,detail,source,kp_type,freq,mastery)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                         (course_id, anchor_name, '章节',
                         f"第{section['number']}章 {section['title']}",
                         chapter_key, section['section_key'], gist, detail,
                         source, 'section', '中频', '未读'),
                    )
                    anchor_id = cur.lastrowid
                    created += 1
                    conn.execute(
                        '''INSERT OR IGNORE INTO kp_identities(kp_id,node_key) VALUES(?,?)''',
                        (anchor_id, _node_key(course_id, f"{section['section_key']}:{anchor_name}")),
                    )
                    conn.execute(
                        '''INSERT INTO knowledge_node_meta(kp_id,schema_version,domain,source_refs_json,status)
                           VALUES(?,?,?,?, 'confirmed')''',
                        (anchor_id, 'cyberpal.knowledge.v1',
                         domain_profiles.profile_for_course(course)['id'],
                         json.dumps([{'evidence_id': item} for item in evidence_ids], ensure_ascii=False)),
                    )
                    conn.executemany(
                        'INSERT OR IGNORE INTO kp_evidence(kp_id,evidence_id,role) VALUES(?,?,?)',
                        [(anchor_id, item, 'supports') for item in evidence_ids],
                    )
                else:
                    anchor_id = anchor['id']
                members = conn.execute(
                    '''SELECT id FROM kp WHERE course_id=? AND section_key=? AND id<>?''',
                    (course_id, section['section_key'], anchor_id),
                ).fetchall()
                for member in members:
                    edge = conn.execute(
                        'INSERT OR IGNORE INTO kp_edges(from_kp,to_kp,kind) VALUES(?,?,?)',
                        (member['id'], anchor_id, 'part_of'),
                    )
                    if edge.rowcount:
                        edge_id = edge.lastrowid
                        conn.execute(
                            '''INSERT OR IGNORE INTO knowledge_relation_meta
                               (edge_id,schema_version,evidence_id,reason,confidence,status)
                               VALUES(?,?,?,?,?,?)''',
                            (edge_id, 'cyberpal.knowledge.v1', evidence_ids[0] if evidence_ids else None,
                             '知识点属于教材小节的结构关系', 0.9, 'confirmed'),
                        )
                        relations += 1
        return {'created': created, 'relations': relations}
    finally:
        conn.close()


def graph_of(course: str) -> dict:
    """图谱数据：节点 + 边，带内按 band 分组。

    Pure read: derived structure (catalog rows, node metadata, stable keys) is
    materialized by sync_memory_views on write events; this function resolves
    the same view in memory and never issues a write.
    """
    conn = connect()
    try:
        cid_row = conn.execute("SELECT id FROM courses WHERE name = ?", (course,)).fetchone()
        if not cid_row:
            return {"nodes": [], "edges": [], "bands": []}
        cid = cid_row["id"]
        fallback_domain = domain_profiles.profile_for_course(course)['id']
        # O-DATA-5: only an explicitly selected (active) catalog drives graph
        # navigation. Candidate/historical uploads never imply readiness.
        catalog_source = conn.execute(
            """SELECT id FROM source_files WHERE course_id=? AND status='indexed'
               AND source_kind='course_catalog' AND catalog_status='active' LIMIT 1""",
            (cid,)).fetchone()
        catalog_uploaded = conn.execute(
            """SELECT 1 FROM source_files WHERE course_id=? AND source_kind='course_catalog'
               AND status='indexed'""",
            (cid,)).fetchone() is not None
        chapters = _chapter_catalog_rows(conn, cid)
        if catalog_uploaded and not catalog_source:
            # Rows materialized by older auto-selection releases must not keep
            # navigating before the user picks a catalog.
            chapters = []
        effective, _ = _node_structure_rows(conn, cid, chapters)
        nodes = [dict(r) for r in conn.execute(
            """SELECT kp.id, kp.name, ki.node_key, band, chapter, chapter_key, section_key,
                      gist, detail, trap, source, related, kp_type, freq, mastery,
                      COALESCE(
                          (SELECT sf.source_kind
                             FROM kp_evidence ke
                             JOIN evidence e ON e.id=ke.evidence_id
                             JOIN source_files sf ON sf.id=e.source_id
                            WHERE ke.kp_id=kp.id
                            ORDER BY ke.evidence_id LIMIT 1),
                          (SELECT sf.source_kind FROM source_files sf
                            WHERE sf.course_id=kp.course_id AND sf.path=kp.source LIMIT 1),
                          ''
                      ) AS source_kind
               FROM kp LEFT JOIN kp_identities ki ON ki.kp_id=kp.id
               WHERE kp.course_id = ? AND COALESCE(kp.deleted_at,'')=''
               ORDER BY kp.id""", (cid,)).fetchall()]
        for node in nodes:
            resolved = effective.get(node['id'])
            if resolved is not None:
                node['chapter_key'], node['section_key'] = resolved
            if not node.get('node_key'):
                # Stable identity may only be persisted by a write event; the
                # read path still exposes the deterministic key.
                node['node_key'] = _node_key(cid, node['name'])
        nodes = [node for node in nodes if not chapter_catalog.is_structural_node(node, node.get('source_kind', ''))]
        # P2-3: user-visible source_refs must not point at retired evidence.
        live_evidence = {row['id'] for row in conn.execute(
            '''SELECT e.id FROM evidence e JOIN source_files sf ON sf.id=e.source_id
               WHERE sf.course_id=? AND COALESCE(e.stale,0)=0''', (cid,)).fetchall()}
        for node in nodes:
            meta = conn.execute('SELECT * FROM knowledge_node_meta WHERE kp_id=?', (node['id'],)).fetchone()
            node['domain'] = meta['domain'] if meta else fallback_domain
            node['schema_version'] = meta['schema_version'] if meta else 'cyberpal.knowledge.v1'
            node['attributes'] = json.loads(meta['attributes_json']) if meta else {}
            refs = json.loads(meta['source_refs_json']) if meta else []
            node['source_refs'] = [ref for ref in refs
                                   if isinstance(ref, dict) and ref.get('evidence_id') in live_evidence]
            node['aliases'] = [item['alias'] for item in conn.execute(
                'SELECT alias FROM kp_aliases WHERE kp_id=? ORDER BY id', (node['id'],)
            ).fetchall()]
            if not node['source_refs']:
                node['source_refs'] = [dict(item) for item in conn.execute(
                    '''SELECT ke.evidence_id,ke.role,e.locator_type,e.locator_start,e.locator_end,e.content
                       FROM kp_evidence ke JOIN evidence e ON e.id=ke.evidence_id
                       WHERE ke.kp_id=? AND COALESCE(e.stale,0)=0 ORDER BY e.id''', (node['id'],)
                ).fetchall()]
        ids = {n["id"] for n in nodes}
        edges = [dict(r) for r in conn.execute(
            """SELECT e.id, e.from_kp, e.to_kp, e.kind, rm.evidence_id,
                      rm.reason, rm.confidence, rm.status AS contract_status
               FROM kp_edges e LEFT JOIN knowledge_relation_meta rm ON rm.edge_id=e.id
               WHERE e.from_kp IN (SELECT id FROM kp WHERE course_id=?)
                 AND e.to_kp IN (SELECT id FROM kp WHERE course_id=?)""", (cid, cid)).fetchall()]
        edges = [e for e in edges if e["from_kp"] in ids and e["to_kp"] in ids]
        bands = [{"code": band, "count": count} for band, count in sorted(
            ((code, sum(1 for node in nodes if node['band'] == code))
             for code in {node['band'] for node in nodes}), key=lambda item: item[0]
        )]
        chapters = _graph_chapters(conn, cid, nodes,
                                   allow_topic_clusters=not catalog_uploaded,
                                   chapters=chapters)
        return {"nodes": nodes, "edges": edges, "bands": bands,
                "chapters": chapters,
                "catalog_status": 'ready' if chapters else ('empty' if catalog_source else 'missing'),
                "chapter_counts": {chapter['key']: chapter['node_count'] for chapter in chapters}}
    finally:
        conn.close()


def list_events(course: str | None = None, workspace: str | None = None) -> list[dict]:
    sql = "SELECT e.*, COALESCE(w.key,'studypal') AS workspace_key FROM events e LEFT JOIN workspaces w ON w.id=e.workspace_id WHERE 1=1"
    args: list = []
    if course:
        sql += " AND e.course_name = ?"
        args.append(course)
    if workspace is not None:
        sql += """ AND COALESCE(w.key,'studypal') = ?
                   AND NOT EXISTS (
                       SELECT 1 FROM courses c LEFT JOIN workspaces cw ON cw.id=c.workspace_id
                       WHERE (c.id=e.course_id OR c.name=e.course_name)
                         AND COALESCE(cw.key,'studypal')<>?)"""
        args.extend((workspace, workspace))
    sql += " ORDER BY e.date"
    conn = connect()
    try:
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
        if not course:
            rows.extend(workspace_task_events(workspace))
        return sorted(rows, key=lambda row: row['date'])
    finally:
        conn.close()


def workspace_task_events(workspace: str | None = None) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute('''SELECT i.*,w.key AS workspace_key FROM workspace_items i
            JOIN workspaces w ON w.id=i.workspace_id
            WHERE i.kind='task' AND i.status='open' AND i.due_at!=''
              AND (? IS NULL OR w.key=?)''', (workspace,workspace)).fetchall()
        return [{'id':f"task-{r['id']}", 'item_id':r['id'], 'title':r['title'],
                 'date':r['due_at'][:10], 'category':'personal', 'note':r['body'],
                 'course_name':'', 'time_range':'', 'location':'',
                 'workspace_key':r['workspace_key']} for r in rows]
    finally:
        conn.close()


def list_notices(course: str | None = None, workspace: str | None = None) -> list[dict]:
    """List imported course or school notices without mixing them into events."""
    sql = "SELECT n.*, COALESCE(w.key,'studypal') AS workspace_key FROM notices n LEFT JOIN courses c ON c.id=n.course_id LEFT JOIN workspaces w ON w.id=c.workspace_id WHERE 1=1"
    args: list = []
    if course:
        sql += " AND n.course_name = ?"
        args.append(course)
    if workspace is not None:
        sql += " AND COALESCE(w.key,'studypal') = ?"; args.append(workspace)
    sql += " ORDER BY n.date DESC, n.id DESC"
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


EVENT_FIELDS = {'record_id', 'course_id', 'workspace_id', 'course_name', 'title', 'category',
                'type_raw', 'date', 'time_range', 'location', 'note', 'source',
                'source_uid', 'source_updated_at'}
EVENT_CATEGORIES = {'class', 'hw', 'exam', 'personal'}


def _validate_event(values: dict) -> dict:
    patch = {key: values[key] for key in values if key in EVENT_FIELDS}
    if 'category' in patch and patch['category'] not in EVENT_CATEGORIES:
        raise ValueError('日程类型只能是 class、hw、exam 或 personal')
    if 'date' in patch:
        try:
            datetime.strptime(str(patch['date']), '%Y-%m-%d')
        except ValueError as exc:
            raise ValueError('日期必须是 YYYY-MM-DD') from exc
    if 'title' in patch and not str(patch['title']).strip():
        raise ValueError('事项标题不能为空')
    return {key: str(value or '').strip() if key not in {'course_id', 'workspace_id'} else value
            for key, value in patch.items()}


def _event_course_scope(conn, values: dict) -> None:
    course_id, course_name = values.get('course_id'), values.get('course_name')
    if course_id is not None:
        course = conn.execute('SELECT id,name,workspace_id FROM courses WHERE id=?', (course_id,)).fetchone()
        if not course:
            raise ValueError('课程不存在')
        if course_name and course_name != course['name']:
            raise ValueError('课程编号与名称不一致')
    else:
        course = conn.execute('SELECT id,name,workspace_id FROM courses WHERE name=?', (course_name,)).fetchone()
    if course:
        workspace_id = course['workspace_id'] or _workspace_id('studypal')
        if values.get('workspace_id') is not None and values['workspace_id'] != workspace_id:
            raise ValueError('课程不属于当前工作区')
        values.update(course_id=course['id'], course_name=course['name'], workspace_id=workspace_id)
    elif values.get('workspace_id') is None:
        values['workspace_id'] = _workspace_id('studypal')
    if not conn.execute("SELECT id FROM workspaces WHERE id=? AND status='active'",
                        (values['workspace_id'],)).fetchone():
        raise ValueError('工作区不存在')


def create_event(values: dict, origin: str = 'manual') -> dict:
    patch = _validate_event(values)
    if 'date' not in patch or 'title' not in patch:
        raise ValueError('事项需要标题和日期')
    conn = connect()
    try:
        _event_course_scope(conn, patch)
        columns = list(patch)
        placeholders = ','.join('?' for _ in columns)
        cur = conn.execute(f"INSERT INTO events ({','.join(columns)}) VALUES ({placeholders})",
                           [patch[key] for key in columns])
        event_id = cur.lastrowid
        row = conn.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
        conn.execute('INSERT INTO event_revisions(event_id,action,old_json,new_json,origin) VALUES(?,?,?,?,?)',
                     (event_id, 'create', '{}', json.dumps(dict(row), ensure_ascii=False), origin))
        conn.commit()
        return dict(row)
    finally:
        conn.close()


def update_event(event_id: int, values: dict, origin: str = 'manual') -> dict:
    patch = _validate_event(values)
    conn = connect()
    try:
        old = conn.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
        if not old:
            raise ValueError('日程不存在')
        if not patch:
            raise ValueError('没有可修改的日程字段')
        if {'course_id', 'course_name', 'workspace_id'} & patch.keys():
            scoped = dict(old)
            scoped.update(patch)
            if 'course_name' in patch and 'course_id' not in patch:
                scoped['course_id'] = None
            elif 'course_id' in patch and 'course_name' not in patch:
                scoped['course_name'] = ''
            _event_course_scope(conn, scoped)
            patch.update({key: scoped[key] for key in ('course_id', 'course_name', 'workspace_id')})
        sets = ','.join(f'{key}=?' for key in patch)
        conn.execute(f'UPDATE events SET {sets} WHERE id=?', [*patch.values(), event_id])
        new = conn.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
        conn.execute('INSERT INTO event_revisions(event_id,action,old_json,new_json,origin) VALUES(?,?,?,?,?)',
                     (event_id, 'update', json.dumps(dict(old), ensure_ascii=False),
                      json.dumps(dict(new), ensure_ascii=False), origin))
        conn.commit()
        return dict(new)
    finally:
        conn.close()


def delete_event(event_id: int, origin: str = 'manual') -> bool:
    conn = connect()
    try:
        old = conn.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
        if not old:
            return False
        conn.execute('DELETE FROM events WHERE id=?', (event_id,))
        conn.execute('INSERT INTO event_revisions(event_id,action,old_json,new_json,origin) VALUES(?,?,?,?,?)',
                     (event_id, 'delete', json.dumps(dict(old), ensure_ascii=False), '{}', origin))
        conn.commit()
        return True
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 结构化日程 / 作业检索
# ---------------------------------------------------------------------------
# 日历和作业不是知识点，不能只依赖 kp 的 LIKE 检索。这里把 events 和
# progress 统一成 Agent 可直接使用的「时间记忆」结果。
_AGENDA_HINT_RE = re.compile(
    r"作业|任务|待办|代办|截止|交作业|提交|上课|课程|日程|日历|安排|考试|什么时候"
)
_TASK_HINT_RE = re.compile(r"作业|任务|待办|代办|截止|交作业|提交")


def _date_from_value(value: str | None) -> date | None:
    """Read the date prefix used by both events and progress tables."""
    if not value:
        return None
    m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", str(value))
    if not m:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(value))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _agenda_range(question: str, today: date | None = None) -> tuple[date | None, date | None, str]:
    """Resolve the human time phrase in an agenda question.

    The result is inclusive. A missing end means "from start onward" and is
    used for questions such as "还有哪些作业要交".
    """
    text = (question or "").strip().replace("／", "/")
    today = today or date.today()

    year_match = re.search(r"(20\d{2})年?", text)
    year = int(year_match.group(1)) if year_match else today.year

    # 「国庆 / 十一期间」按中国通常的 10 月 1 日至 7 日处理；若用户写了年份，
    # 年份来自问题，否则使用当前年份。
    if re.search(r"国庆|十一(?:期间|假期|长假)?", text):
        holiday_start = date(year, 10, 1)
        holiday_end = date(year, 10, 7)
        return holiday_start, holiday_end, f"{year}年国庆期间（10月1日—10月7日）"

    explicit = re.search(r"(20\d{2})[-/]([01]?\d)[-/]([0-3]?\d)|"
                         r"(20\d{2})年([01]?\d)月([0-3]?\d)日?", text)
    if explicit:
        parts = explicit.groups()
        if parts[0]:
            d = date(int(parts[0]), int(parts[1]), int(parts[2]))
        else:
            d = date(int(parts[3]), int(parts[4]), int(parts[5]))
        return d, d, d.strftime("%Y年%m月%d日")

    month_day = re.search(r"([01]?\d)月([0-3]?\d)(?:日|号)?", text)
    if month_day:
        d = date(year, int(month_day.group(1)), int(month_day.group(2)))
        return d, d, d.strftime("%Y年%m月%d日")

    day_only = re.search(r"(?<!\d)([0-3]?\d)(?:日|号)(?!\d)", text)
    if day_only:
        d = date(year, today.month, int(day_only.group(1)))
        return d, d, d.strftime("%Y年%m月%d日")

    if re.search(r"从今天起|从今天开始|今天之后|往后", text):
        return today, None, f"从今天（{today:%Y年%m月%d日}）起"
    if re.search(r"今天|今日", text):
        return today, today, "今天"
    if re.search(r"明天|明日", text):
        d = today + timedelta(days=1)
        return d, d, "明天"
    if re.search(r"后天", text):
        d = today + timedelta(days=2)
        return d, d, "后天"
    if re.search(r"接下来一周|未来一周|最近一周|近一周", text):
        end = today + timedelta(days=7)
        return today, end, f"{today:%Y年%m月%d日}至{end:%Y年%m月%d日}"
    if re.search(r"本周|这周|这一周", text):
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
        return start, end, f"本周（{start:%Y年%m月%d日}至{end:%Y年%m月%d日}）"
    if re.search(r"下周|下一周", text):
        start = today - timedelta(days=today.weekday()) + timedelta(days=7)
        end = start + timedelta(days=6)
        return start, end, f"下周（{start:%Y年%m月%d日}至{end:%Y年%m月%d日}）"

    if _TASK_HINT_RE.search(text):
        return today, None, f"从今天（{today:%Y年%m月%d日}）起"
    if _AGENDA_HINT_RE.search(text):
        return today, today, "今天"
    return None, None, ""


def _occurrence_key(title: str) -> str:
    match = re.search(r"第\s*(\d+)\s*[次讲]", title or "")
    return match.group(1) if match else ""


def query_agenda(question: str, course: str | None = None, workspace: str | None = None) -> dict:
    """Query deadline/calendar memory for a natural-language question.

    ``has_query`` deliberately stays false for ordinary knowledge questions so
    the general Agent can converse normally without every message becoming a
    calendar lookup.
    """
    if not _AGENDA_HINT_RE.search(question or ""):
        return {"has_query": False, "count": 0, "items": [], "next_items": []}

    start, end, range_label = _agenda_range(question)
    if start is None:
        return {"has_query": False, "count": 0, "items": [], "next_items": []}

    task_mode = bool(_TASK_HINT_RE.search(question or ""))
    task_only = task_mode and not re.search(r"上课|课程|日程|日历|安排|考试", question or "")
    conn = connect()
    try:
        event_rows = conn.execute(
            """SELECT e.id, e.course_name, e.title, e.category, e.date,
                      e.time_range, e.location, e.note
                 FROM events e LEFT JOIN workspaces w ON w.id=e.workspace_id
                WHERE (? IS NULL OR e.course_name = ?)
                  AND (? IS NULL OR COALESCE(w.key,'studypal') = ?)
                  AND (? = 0 OR e.category = 'hw' OR e.type_raw LIKE '%作业%')
                ORDER BY e.date, e.id""",
            (course, course, workspace, workspace, 1 if task_only else 0),
        ).fetchall()
        progress_rows = conn.execute(
            """SELECT p.id, c.name AS course_name, p.seq, p.homework,
                      p.due_at, p.submit_via, p.status, p.date, p.note
                 FROM progress p JOIN courses c ON c.id = p.course_id
                 LEFT JOIN workspaces w ON w.id=c.workspace_id
                WHERE (? IS NULL OR c.name = ?)
                  AND (? IS NULL OR COALESCE(w.key,'studypal') = ?)
                  AND (COALESCE(p.homework, '') <> '' OR COALESCE(p.due_at, '') <> '')
                ORDER BY COALESCE(p.due_at, p.date), p.id""",
            (course, course, workspace, workspace),
        ).fetchall()
    finally:
        conn.close()

    items: list[dict] = []
    seen: dict[tuple[str, str, str], dict] = {}

    def in_range(d: date | None) -> bool:
        return bool(d and d >= start and (end is None or d <= end))

    for row in event_rows:
        d = _date_from_value(row[4])
        if not in_range(d):
            continue
        item = {
            "id": row[0], "source_type": "event", "course_name": row[1] or "",
            "title": row[2] or "未命名日程", "date": d.isoformat(),
            "time": row[5] or "", "location": row[6] or "",
            "status": "未标记", "submit_via": "", "note": row[7] or "",
            "source": "日历",
        }
        key = (item["course_name"], item["date"], _occurrence_key(item["title"]) or item["title"])
        seen[key] = item

    for row in progress_rows:
        d = _date_from_value(row[4]) or _date_from_value(row[7])
        if not in_range(d):
            continue
        title = row[3] or row[2] or "未命名作业"
        key = (row[1] or "", d.isoformat(), _occurrence_key(title) or title)
        existing = seen.get(key)
        if existing:
            existing["title"] = existing["title"] if existing["title"] != "未命名作业" else title
            existing["status"] = row[6] or existing["status"]
            existing["submit_via"] = row[5] or existing["submit_via"]
            existing["note"] = "；".join(x for x in (existing["note"], row[8] or "") if x)
            existing["source"] = "日历 + 课程进度"
            continue
        item = {
            "id": row[0], "source_type": "progress", "course_name": row[1] or "",
            "title": title, "date": d.isoformat(), "time": "",
            "location": "", "status": row[6] or "未标记",
            "submit_via": row[5] or "", "note": row[8] or "",
            "source": "课程进度",
        }
        seen[key] = item

    items = sorted(seen.values(), key=lambda x: (x["date"], x["time"], x["course_name"], x["title"]))
    if not course:
        items.extend({'id':r['id'], 'source_type':'workspace_task', 'course_name':'',
                      'title':r['title'], 'date':r['date'], 'time':'', 'location':'',
                      'note':r['note'], 'status':'未完成', 'submit_via':'', 'source':'工作区待办'}
                     for r in workspace_task_events(workspace) if in_range(_date_from_value(r['date'])))
        items.sort(key=lambda x:(x['date'],x['time'],x['title']))

    next_items: list[dict] = []
    if not items and end is not None:
        # 空结果也给出最近的下一项，帮助用户区分「库里没有」和「日期理解错」。
        future = query_agenda("从今天起有哪些作业要交", course, workspace)
        next_items = [x for x in future.get("items", []) if x["date"] > end.isoformat()][:3]

    empty_label = "作业或待办" if task_mode else "日程"
    return {
        "has_query": True,
        "kind": "tasks" if task_mode else "calendar",
        "range": {"start": start.isoformat(), "end": end.isoformat() if end else None},
        "range_label": range_label,
        "count": len(items),
        "items": items[:50],
        "next_items": next_items,
        "message": None if items else f"{range_label}没有找到已记录的{empty_label}",
    }


def sync_bridge_links() -> None:
    """Resolve legacy bridge text against formal nodes and stable node keys."""
    conn = connect()
    try:
        rows = conn.execute('SELECT * FROM bridges ORDER BY id').fetchall()
        with conn:
            for bridge in rows:
                a = conn.execute('''SELECT kp.id,ki.node_key FROM kp
                                    JOIN courses c ON c.id=kp.course_id
                                    LEFT JOIN kp_identities ki ON ki.kp_id=kp.id
                                    WHERE c.name=? AND kp.name=?''',
                                 (bridge['course_a'], bridge['kp_a'])).fetchone()
                b = conn.execute('''SELECT kp.id,ki.node_key FROM kp
                                    JOIN courses c ON c.id=kp.course_id
                                    LEFT JOIN kp_identities ki ON ki.kp_id=kp.id
                                    WHERE c.name=? AND kp.name=?''',
                                 (bridge['course_b'], bridge['kp_b'])).fetchone()
                status = 'resolved' if a and b else 'unresolved'
                conn.execute('''INSERT INTO bridge_links
                    (bridge_id,a_kp_id,b_kp_id,a_node_key,b_node_key,status,updated_at)
                    VALUES(?,?,?,?,?,?,datetime('now','localtime'))
                    ON CONFLICT(bridge_id) DO UPDATE SET
                    a_kp_id=excluded.a_kp_id,b_kp_id=excluded.b_kp_id,
                    a_node_key=excluded.a_node_key,b_node_key=excluded.b_node_key,
                    status=excluded.status,updated_at=excluded.updated_at''',
                    (bridge['id'], a['id'] if a else None, b['id'] if b else None,
                     (a['node_key'] if a and a['node_key'] else ''),
                     (b['node_key'] if b and b['node_key'] else ''), status))
    finally:
        conn.close()


def list_bridges(course: str | None = None) -> list[dict]:
    """跨课桥接，附正式节点 ID、稳定 key 和解析状态（纯读）。"""
    sql = """SELECT b.*,
                    ka.id AS a_kp_id, kb.id AS b_kp_id,
                    ka.course_id AS a_course_id, ka.name AS a_kp_name,
                    kb.course_id AS b_course_id, kb.name AS b_kp_name,
                    COALESCE(kia.node_key, '') AS a_node_key,
                    COALESCE(kib.node_key, '') AS b_node_key,
                    CASE WHEN ka.id IS NOT NULL AND kb.id IS NOT NULL
                         THEN 'resolved' ELSE 'unresolved' END AS status
             FROM bridges b
             LEFT JOIN courses ca ON ca.name=b.course_a
             LEFT JOIN kp ka ON ka.course_id=ca.id AND ka.name=b.kp_a
                                AND COALESCE(ka.deleted_at,'')=''
             LEFT JOIN courses cb ON cb.name=b.course_b
             LEFT JOIN kp kb ON kb.course_id=cb.id AND kb.name=b.kp_b
                                AND COALESCE(kb.deleted_at,'')=''
             LEFT JOIN kp_identities kia ON kia.kp_id=ka.id
             LEFT JOIN kp_identities kib ON kib.kp_id=kb.id
             WHERE 1=1"""
    args: list = []
    if course:
        sql += " AND (b.course_a = ? OR b.course_b = ?)"
        args += [course, course]
    sql += " ORDER BY b.id"
    conn = connect()
    try:
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()
    for row in rows:
        if not row['a_node_key'] and row['a_kp_id'] is not None:
            row['a_node_key'] = _node_key(row['a_course_id'], row['a_kp_name'])
        if not row['b_node_key'] and row['b_kp_id'] is not None:
            row['b_node_key'] = _node_key(row['b_course_id'], row['b_kp_name'])
        row.pop('a_course_id', None)
        row.pop('a_kp_name', None)
        row.pop('b_course_id', None)
        row.pop('b_kp_name', None)
    return rows


def list_revisions(limit: int = 30) -> list[dict]:
    """写回留痕 —— 编排台的执行痕迹读它。记忆改了必须可追。"""
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT r.*, kp.name AS kp_name, co.name AS course_name
               FROM revisions r
               JOIN kp ON kp.id = r.kp_id
               JOIN courses co ON co.id = kp.course_id
               ORDER BY r.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


SEARCH_GIST_LIMIT = 1200


def _truncated_gist(text) -> tuple[str, bool]:
    """Bound one retrieval snippet and say explicitly whether text was cut."""
    text = str(text or '')
    return text[:SEARCH_GIST_LIMIT], len(text) > SEARCH_GIST_LIMIT


def _search_hit(text, *, ref_kind: str, ref_id, **fields) -> dict:
    gist, truncated = _truncated_gist(text)
    fields.update({'gist': gist, 'ref_kind': ref_kind, 'ref_id': ref_id,
                   'truncated': truncated})
    return fields


def dedupe_search_hits(hits: list[dict]) -> list[dict]:
    """Keep the first (most relevant) hit per (ref_kind, ref_id) pair.

    The public 'kind' is intentionally coarse for old clients ('file' covers
    source chunks and evidence), so precise provenance keys win when present;
    legacy callers without them fall back to (kind, id).
    """
    seen: set[tuple[str, str]] = set()
    unique: list[dict] = []
    for hit in hits:
        key = (str(hit.get('ref_kind') or hit.get('kind') or ''),
               str(hit.get('ref_id', hit.get('id'))))
        if key in seen:
            continue
        seen.add(key)
        unique.append(hit)
    return unique


def memory_search_hits(query: str, limit: int = 10, course: str | None = None,
                       workspace: str | None = None, *, include_dev: bool = False) -> list[dict]:
    """Combined local retrieval for /api/search, /ask context and memory tools.

    FTS/LIKE recall can return the same source chunk/evidence from several
    paths; this keeps the best-ranked occurrence per (kind, ref_id) so
    duplicates never consume the caller's result slots.
    """
    from . import sources
    hits = search(query, limit, course, workspace, include_dev=include_dev)
    hits += sources.search(query, course, limit, workspace, include_dev=include_dev)
    return dedupe_search_hits(hits)[:limit]


def search(query: str, limit: int = 10, course: str | None = None,
           workspace: str | None = None, *, include_dev: bool = False) -> list[dict]:
    """检索即记忆的入口。首期用字段匹配；阶段 2 换全文 + 向量双路。

    返回的每一项都带 source —— 这是「回答带引用」的地基。
    course=None 表示跨全部课程检索（全局范围）。

    支持多关键词：「A 和 B 有关系吗」会把 A、B 分别查再合并，
    否则跨课问题只能命中一侧。
    """
    # Accept the natural ``search(query, course)`` form as well as the legacy
    # ``search(query, limit, course)`` form used by the API layer.
    if isinstance(limit, str) and course is None:
        course, limit = limit, 10
    limit = max(1, min(int(limit), 50))
    personal_hits = []
    if not course:
        terms = keywords_of(query) or [query]
        conn = connect()
        try:
            clauses = ' OR '.join('(i.title LIKE ? OR i.body LIKE ?)' for _ in terms)
            rows = conn.execute(f'''SELECT i.*,w.key FROM workspace_items i
                JOIN workspaces w ON w.id=i.workspace_id
                WHERE (? IS NULL OR w.key=?) AND ({clauses}) ORDER BY i.updated_at DESC LIMIT ?''',
                [workspace,workspace,*[v for term in terms for v in (f'%{term}%',f'%{term}%')],limit]).fetchall()
            personal_hits = [_search_hit(
                r['body'], ref_kind='workspace_item', ref_id=r['id'],
                id=r['id'], kind='workspace_item', name=r['title'],
                detail='', mastery='', chapter=r['kind'], course_name='',
                source=r['path'] or f"{r['key']}/items/{r['id']}") for r in rows]
        finally:
            conn.close()
    # FTS is the first recall path. The existing field search remains the
    # deterministic fallback and preserves the old ranking semantics.
    try:
        from . import search_index
        indexed = search_index.search(query, course, limit, workspace,
                                      include_dev=include_dev)
    except Exception:
        indexed = []
    if indexed:
        conn = connect()
        try:
            hits = []
            seen_refs: set[tuple[str, str]] = set()
            for item in indexed:
                ref_key = (str(item.get('kind') or ''), str(item.get('ref_id') or ''))
                if ref_key in seen_refs:
                    continue
                seen_refs.add(ref_key)
                if item['kind'] == 'kp':
                    # 026: a frozen 011 trigger can keep a soft-deleted kp in
                    # search_fts, so the FTS hit must re-check deleted_at.
                    row = conn.execute("""SELECT kp.id,kp.name,kp.gist,kp.detail,kp.trap,kp.source,
                                               kp.mastery,kp.freq,kp.chapter,co.name AS course_name
                                        FROM kp JOIN courses co ON co.id=kp.course_id
                                       WHERE kp.id=? AND COALESCE(kp.deleted_at,'')=''""",
                                       (int(item['ref_id']),)).fetchone()
                    if row and (not workspace or row['course_name'] in _workspace_course_names(workspace)):
                        hit = dict(row)
                        hit['kind'] = 'kp'
                        hit['ref_kind'] = 'kp'
                        hit['ref_id'] = int(item['ref_id'])
                        hit['truncated'] = len(str(hit.get('gist') or '')) > SEARCH_GIST_LIMIT
                        hits.append(hit)
                elif item['kind'] == 'source_chunk':
                    # 022: FTS can lag a rescan; never serve retired chunks.
                    row = conn.execute("""SELECT sc.id,sc.content,sf.path,co.name AS course_name
                                        FROM source_chunks sc JOIN source_files sf ON sf.id=sc.source_id
                                        JOIN courses co ON co.id=sf.course_id
                                       WHERE sc.id=? AND sf.status='indexed' AND sc.stale=0""",
                                       (int(item['ref_id']),)).fetchone()
                    if row and (not workspace or row['course_name'] in _workspace_course_names(workspace)):
                        hits.append(_search_hit(
                            row['content'], ref_kind='source_chunk', ref_id=int(item['ref_id']),
                            id=row['id'], name=Path(row['path']).name, detail='',
                            source=row['path'], course_name=row['course_name'],
                            chapter='课程文件', mastery='', kind='file'))
                elif item['kind'] == 'evidence':
                    # 022: retired evidence is historical, not current support.
                    row = conn.execute("""SELECT e.id,e.content,sf.path,co.name AS course_name,
                                               e.locator_type,e.locator_start,e.locator_end
                                        FROM evidence e JOIN source_files sf ON sf.id=e.source_id
                                        JOIN courses co ON co.id=sf.course_id
                                       WHERE e.id=? AND sf.status='indexed' AND e.stale=0""",
                                       (int(item['ref_id']),)).fetchone()
                    if row and (not workspace or row['course_name'] in _workspace_course_names(workspace)):
                        locator = f"{row['locator_type']} {row['locator_start'] or ''}".strip()
                        hits.append(_search_hit(
                            row['content'], ref_kind='evidence', ref_id=int(item['ref_id']),
                            id=row['id'], name=Path(row['path']).name, detail='',
                            source=f"{row['path']} ({locator})",
                            course_name=row['course_name'], chapter='证据定位',
                            mastery='', kind='file'))
                elif item['kind'] == 'event':
                    row = conn.execute("""SELECT id,course_name,title,category,date,time_range,location,note
                                           FROM events WHERE id=?""", (int(item['ref_id']),)).fetchone()
                    if row and (not workspace or _event_in_workspace(conn, row['id'], workspace)):
                        hits.append(_search_hit(
                            row['note'] or row['title'], ref_kind='event', ref_id=int(item['ref_id']),
                            id=row['id'], name=row['title'], detail='',
                            source=f"日历 {row['date']}", course_name=row['course_name'] or '',
                            chapter=row['category'] or '日程', mastery='', kind='event'))
                elif item['kind'] == 'notice':
                    row = conn.execute("""SELECT id,course_name,title,body,date,source,url
                                           FROM notices WHERE id=?""", (int(item['ref_id']),)).fetchone()
                    if row and (not workspace or row['course_name'] in _workspace_course_names(workspace)):
                        source = f"{row['source'] or '通知'} {row['date']}".strip()
                        hits.append(_search_hit(
                            row['body'] or row['title'], ref_kind='notice', ref_id=int(item['ref_id']),
                            id=row['id'], name=row['title'], detail='', source=source,
                            course_name=row['course_name'] or '', chapter='官方通知',
                            mastery='', kind='notice', url=row['url'] or ''))
            if hits:
                return dedupe_search_hits(personal_hits + hits)[:limit]
        finally:
            conn.close()
    kws = keywords_of(query) or [query]
    seen: set[tuple[str, str]] = set()
    hits: list[dict] = []
    # 每个词都取满 limit，合并后再截断 —— 否则第一个词就把名额占完，
    # 「A 和 B 的关系」这类跨课问题会看不到 B 那侧。
    for kw in kws:
        if not kw or len(kw) < 2:
            continue
        for row in _search_one(kw, limit, course, workspace, include_dev):
            key = (str(row.get('ref_kind') or row.get('kind') or ''),
                   str(row.get('ref_id', row.get('id'))))
            if key not in seen:
                seen.add(key)
                hits.append(row)
    # 没有主题词命中时保持空结果。按问句前缀硬截取会把「什么」「组织」
    # 之类的泛词当成证据，污染开放问答；口语化召回交给后续全文/向量索引。
    # 跨课优先：让命中的课程都能露头，再截断
    if len({h["course_name"] for h in hits}) > 1:
        by_course: dict[str, list[dict]] = {}
        for h in hits:
            by_course.setdefault(h["course_name"], []).append(h)
        merged: list[dict] = []
        while len(merged) < limit and any(by_course.values()):
            for lst in by_course.values():
                if lst and len(merged) < limit:
                    merged.append(lst.pop(0))
        return dedupe_search_hits(personal_hits + merged)[:limit]
    return dedupe_search_hits(personal_hits + hits)[:limit]


def _workspace_course_names(workspace: str) -> set[str]:
    conn = connect()
    try:
        return {row['name'] for row in conn.execute(
            """SELECT c.name FROM courses c LEFT JOIN workspaces w ON w.id=c.workspace_id
               WHERE COALESCE(w.key,'studypal')=?""", (workspace,)).fetchall()}
    finally:
        conn.close()


def _event_in_workspace(conn, event_id: int, workspace: str) -> bool:
    row = conn.execute(
        """SELECT COALESCE(w.key,'studypal') AS workspace_key FROM events e
           LEFT JOIN workspaces w ON w.id=e.workspace_id WHERE e.id=?""", (event_id,)
    ).fetchone()
    return bool(row and row['workspace_key'] == workspace)


def _search_one(query: str, limit: int, course: str | None, workspace: str | None = None,
                include_dev: bool = False) -> list[dict]:
    q = f"%{query}%"
    # T2.21/T2.9: soft-deleted nodes and development-doc imports stay out of the
    # field-search fallback even when FTS is unavailable (include_dev is debug).
    sql = """SELECT kp.id, kp.name, kp.gist, kp.detail, kp.trap, kp.source,
                    kp.mastery, kp.freq, kp.chapter, co.name AS course_name
             FROM kp JOIN courses co ON co.id = kp.course_id
             WHERE COALESCE(kp.deleted_at,'')=''
               AND (kp.name LIKE ? OR kp.gist LIKE ? OR kp.detail LIKE ? OR kp.related LIKE ?)"""
    args: list = [q, q, q, q]
    if not include_dev:
        sql += """ AND NOT EXISTS (SELECT 1 FROM source_files dev_sf
                                    WHERE dev_sf.source_kind='development_doc'
                                      AND dev_sf.path=kp.source)"""
    if course:
        sql += " AND co.name = ?"
        args.append(course)
    if workspace:
        sql += " AND COALESCE((SELECT key FROM workspaces WHERE id=co.workspace_id),'studypal') = ?"
        args.append(workspace)
    sql += " LIMIT ?"
    args.append(limit)
    conn = connect()
    try:
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()
    hits = []
    for row in rows:
        row['gist'], truncated = _truncated_gist(row.get('gist'))
        row.update({'ref_kind': 'kp', 'ref_id': row['id'], 'truncated': truncated})
        hits.append(row)
    return hits


# ── 关键词抽取 ────────────────────────────────────────
# 与前端 web/index.html 的 keywordsOf 保持同一套口径：
# 前端为了即时反馈先在本地查，后端为 /api/agent/ask 再查一次，两边不能各说各话。

_SHELL = [
    # 出题类（长串在前，否则「举个例子」会被「举个例」吃掉半截）
    r"给我出一道[^？?，,。]*的?题", r"帮我出一道[^？?，,。]*的?题", r"出一道[^？?，,。]*的?题",
    r"给我出题", r"帮我出题", r"出个题", r"出道题", r"出一道题", r"来个题", r"来道题",
    r"考考我", r"考我一下",
    r"请用[^？?，,。]{0,10}解释", r"通俗地?解释", r"请解释",
    r"举个例子说明", r"举个例子", r"举个例", r"举例说明", r"举例",
    r"说说看", r"说说", r"讲讲", r"解释一下", r"介绍一下", r"告诉我",
    # 疑问式
    r"有什么关系", r"有什么联系", r"什么是", r"有哪几种", r"有哪几个", r"有哪些", r"有那些", r"是什么", r"是啥", r"怎么样",
    r"怎么回事", r"怎么算", r"怎么看", r"如何理解", r"什么意思", r"是指什么",
    r"包括哪些", r"有重叠", r"有关系", r"有联系", r"相关吗", r"有什么区别",
    r"的区别", r"考不考", r"考什么", r"考哪些",
]
_SHELL_RE = re.compile("|".join(_SHELL))
_PUNCT_RE = re.compile(r"[？?。，,！!、；;：:（）()「」【】“”\"']")
_LEAD_RE = re.compile(
    r"^(帮我|请问|问一下|我想问|我要问|麻烦|想问下|想问一下|说说|讲讲|解释一下|介绍一下|我想知道|我想了解)"
)
_CONJ_RE = re.compile(r"[和与跟及]|以及|还有")
_QUIZ_RE = re.compile(r"(?:出|来|考)[^，,。？?]*?[道的]?\s*([^，,。？?]{2,14}?)\s*(?:的)?题(?:目|吗)?$")

_STOP = {
    "的", "有", "哪", "哪些", "什么", "怎么", "如何", "帮我", "一下", "请问",
    "这门课", "这课", "本课", "课程", "是", "了", "吗", "呢", "个", "我", "要",
    "看", "该", "时候", "都", "还", "在", "这", "那", "几个", "几种", "可以",
    "能", "和", "与", "以及", "跟", "同", "地方", "方面", "东西", "关系", "联系",
    "重叠", "相关", "之间", "哪些方面", "一道", "一个", "一条", "一份", "一道题",
    "题", "请问下",
}

_CORE_STEPS = [
    re.compile(r"的(题|题吗|话|事情|内容|东西)$"),
    re.compile(r"^(关于|对于|有关|一道|一个|一条|一份|一种|一类|几个|个|道)"),
    re.compile(r"^[的了着过把给对向从和与跟及]"),
    re.compile(r"的+$"),
]


def core_of(x: str) -> str:
    """剥掉前后缀虚词，剥到稳定为止（单遍只啃一层）。"""
    c = (x or "").strip()
    for _ in range(4):
        before = c
        for rx in _CORE_STEPS:
            c = rx.sub("", c).strip()
        if c == before:
            break
    return c


def keywords_of(text: str) -> list[str]:
    """从自然语言里抽可检索的关键词，按优先级排。"""
    s = _PUNCT_RE.sub(" ", text or "").strip()
    quiz_kw = ""
    m = _QUIZ_RE.search(s)
    if m:
        quiz_kw = core_of(m.group(1))
    s = _SHELL_RE.sub(" ", s)
    s = _LEAD_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()

    parts = [p.strip() for p in _CONJ_RE.split(s) if p.strip()]

    cands: list[str] = []

    def push(c: str) -> None:
        c = core_of(c).lstrip("这那本").strip()
        if 2 <= len(c) <= 14 and c not in _STOP and c not in cands:
            cands.append(c)

    def push_split(c: str) -> None:
        chunks = [core_of(z) for z in re.split(r"的|说明|关于|中|里", core_of(c))]
        chunks = [z for z in chunks if 2 <= len(z) <= 14 and z not in _STOP]
        chunks.sort(key=len, reverse=True)
        if chunks:
            push(chunks[0])

    for part in parts:
        # 中文短语通常没有空格；先按「组织中的双元能力」这类结构切一次，
        # 再处理空格词，避免整句外壳成为唯一检索词。
        push_split(part)
        toks = [t for t in part.split() if len(t) >= 2 and t not in _STOP]
        toks = [t for t in toks if not re.fullmatch(r"[a-zA-Z]{1,2}", t)]
        toks.sort(key=len, reverse=True)
        for tok in toks:
            cleaned = core_of(tok)
            if 2 <= len(cleaned) <= 14:
                push(cleaned)
                if cleaned != tok:
                    push_split(tok)
            else:
                push_split(cleaned)

    picked: list[str] = []
    for part in parts:
        seg = [core_of(z) for z in part.split()]
        seg = [z for z in seg if len(z) >= 2 and z not in _STOP]
        seg.sort(key=len, reverse=True)
        if seg and len(seg[0]) <= 14 and seg[0] not in picked:
            picked.append(seg[0])

    if s:
        whole = core_of(s.split()[0])
        if 2 <= len(whole) <= 14 and whole not in _STOP:
            cands.append(whole)

    out: list[str] = []
    for c in [quiz_kw, *picked, *cands]:
        if c and c not in out:
            out.append(c)

    if not out:
        fb = core_of(s)[:14] or core_of(_PUNCT_RE.sub("", text or ""))[:14]
        if len(fb) >= 2:
            out.append(fb)
    return out


# ── T2.21: user-authored nodes (create / soft delete) ──────────────────
KP_CREATE_LIMITS = {'name': 180, 'gist': 1000, 'detail': 4000, 'trap': 1000,
                    'chapter': 180, 'band': 80, 'kp_type': 40, 'source': 500}


def create_kp(course: str, name: str, *, gist: str = '', detail: str = '', trap: str = '',
              chapter: str = '', band: str = '', kp_type: str = '', source: str = '') -> dict:
    """Create a node from the knowledge page plus its stable kp identity.

    UNIQUE(course_id,name) also covers soft-deleted rows, so a same-name node
    that was soft deleted is revived instead of blocking the user's new node.
    """
    payload = {'name': ' '.join(str(name or '').split()).strip(),
               'gist': str(gist or '').strip(), 'detail': str(detail or '').strip(),
               'trap': str(trap or '').strip(), 'chapter': str(chapter or '').strip(),
               'band': str(band or '').strip(), 'kp_type': str(kp_type or '').strip(),
               'source': str(source or '').strip()}
    if not payload['name']:
        raise ValueError('知识点名称不能为空')
    for field, limit in KP_CREATE_LIMITS.items():
        if len(payload[field]) > limit:
            raise ValueError(f'{field} 内容过长')
    conn = connect()
    try:
        course_row = conn.execute(
            'SELECT id FROM courses WHERE name=?', (str(course or '').strip(),)).fetchone()
        if not course_row:
            raise ValueError('课程不存在')
        course_id = int(course_row['id'])
        existing = conn.execute(
            "SELECT id,COALESCE(deleted_at,'') AS deleted_at FROM kp WHERE course_id=? AND name=?",
            (course_id, payload['name'])).fetchone()
        if existing and not existing['deleted_at']:
            raise ValueError('同一课程中已有同名知识点')
        try:
            with conn:
                if existing:
                    conn.execute(
                        '''UPDATE kp SET gist=?,detail=?,trap=?,chapter=?,band=?,kp_type=?,source=?,
                                        deleted_at='',updated_at=datetime('now','localtime')
                           WHERE id=?''',
                        (payload['gist'], payload['detail'], payload['trap'], payload['chapter'],
                         payload['band'], payload['kp_type'], payload['source'],
                         int(existing['id'])))
                    kp_id = int(existing['id'])
                else:
                    cursor = conn.execute(
                        '''INSERT INTO kp(course_id,name,gist,detail,trap,chapter,band,kp_type,source)
                           VALUES(?,?,?,?,?,?,?,?,?)''',
                        (course_id, payload['name'], payload['gist'], payload['detail'],
                         payload['trap'], payload['chapter'], payload['band'],
                         payload['kp_type'], payload['source']))
                    kp_id = int(cursor.lastrowid)
        except Exception as exc:
            if 'UNIQUE' in str(exc).upper():
                raise ValueError('同一课程中已有同名知识点') from exc
            raise
    finally:
        conn.close()
    ensure_node_identity(kp_id)
    return get_kp(kp_id) or {'id': kp_id, 'name': payload['name'], 'course_id': course_id}


def soft_delete_kp(kp_id: int) -> dict:
    """Soft delete a node (kp.deleted_at); documents keep their kp_id link."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id FROM kp WHERE id=? AND COALESCE(deleted_at,'')=''",
            (int(kp_id),)).fetchone()
        if not row:
            raise ValueError('知识点不存在')
        with conn:
            conn.execute(
                """UPDATE kp SET deleted_at=datetime('now','localtime'),
                                 updated_at=datetime('now','localtime')
                   WHERE id=?""", (int(kp_id),))
        return {'ok': True, 'kp_id': int(kp_id), 'deleted': True}
    finally:
        conn.close()
