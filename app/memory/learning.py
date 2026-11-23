"""Learning events (028/T3.7): sessions, events and quiz attempts.

Single write entry for learning telemetry. Modules that observe learning
(review queues, note editor, material reader, chat) call record_event instead
of writing the tables directly, so idempotency, validation and workspace
scoping stay in one place (B3-EXECUTION-BRIEF §3).

Design notes:
- All timestamps are local time strings matching the rest of the database.
- Retries are deduplicated by optional idempotency_key; the partial unique
  index from 028 keeps key-less events reusable (empty string is not unique).
- append-only: learning_events and quiz_attempts are written here only, no
  update/delete API exists by design.
- Reads (summary/timeline/recent_materials) are workspace scoped; the default
  workspace is user_profile.active_workspace_key (studypal when unset), i.e.
  the same default the rest of the UI uses.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta

from .db import connect

SESSION_KINDS = ('study', 'review', 'read', 'chat')
EVENT_KINDS = (
    'read',
    'open_kp',
    'open_material',
    'excerpt',
    'note_saved',
    'candidate_confirmed',
    'candidate_rejected',
    'review_item',
    'quiz_attempt',
    'mastery_changed',
    'chat_ask',
)
SUMMARY_RANGES = ('today', 'week', 'all')
TIMESTAMP_FORMAT = '%Y-%m-%d %H:%M:%S'
DEFAULT_WORKSPACE = 'studypal'
MAX_PAYLOAD = 4_000
MAX_IDEMPOTENCY = 120
MAX_DURATION = 86_400
MAX_SOURCE = 40
PAYLOAD_PREVIEW = 200
RECENT_MATERIAL_LIMIT = 5
MAX_RECENT_MATERIALS = 50
TIMELINE_LIMIT = 50
MAX_TIMELINE_LIMIT = 200
MATERIAL_KINDS = ('open_material', 'read', 'excerpt')


def _text(value) -> str:
    return ' '.join(str(value or '').split()).strip()


def _int_id(value, label='id') -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{label} 必须是正整数') from exc
    if number <= 0:
        raise ValueError(f'{label} 必须是正整数')
    return number


def _choice(value, allowed, message) -> str:
    text = str(value or '').strip()
    if text not in allowed:
        raise ValueError(message)
    return text


def _clamp(value, low, high, default) -> int:
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError('limit 必须是整数') from exc
    return max(low, min(high, number))


def _timestamp(value) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError('时间格式应为 YYYY-MM-DD HH:MM:SS') from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed.strftime(TIMESTAMP_FORMAT)


def _duration(value) -> int:
    if value is None:
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError('duration_seconds 必须是不小于 0 的整数') from exc
    if number < 0:
        raise ValueError('duration_seconds 必须是不小于 0 的整数')
    if number > MAX_DURATION:
        raise ValueError('单次事件时长不能超过 24 小时')
    return number


def _idempotency(value) -> str:
    text = str(value or '').strip()
    if len(text) > MAX_IDEMPOTENCY:
        raise ValueError(f'idempotency_key 不能超过 {MAX_IDEMPOTENCY} 个字符')
    return text


def _payload(value, source='') -> str:
    if value is None or value == '':
        data = {}
    elif isinstance(value, dict):
        data = dict(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            data = {}
        else:
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError('payload 必须是 JSON 对象') from exc
    else:
        raise ValueError('payload 必须是 JSON 对象')
    if not isinstance(data, dict):
        raise ValueError('payload 必须是 JSON 对象')
    label = _text(source)
    if label and 'source' not in data:
        data['source'] = label
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True)
    if len(encoded) > MAX_PAYLOAD:
        raise ValueError(f'payload 不能超过 {MAX_PAYLOAD} 个字符')
    return encoded


def _active_workspace_key(conn) -> str:
    try:
        row = conn.execute(
            'SELECT active_workspace_key FROM user_profile WHERE id=1'
        ).fetchone()
    except sqlite3.Error:
        return DEFAULT_WORKSPACE
    key = _text(row['active_workspace_key']) if row is not None else ''
    return key or DEFAULT_WORKSPACE


def _workspace(conn, key=None) -> dict:
    wanted = _text(key) or _active_workspace_key(conn)
    row = conn.execute(
        "SELECT id, key, name FROM workspaces WHERE key=? AND status='active'",
        (wanted,),
    ).fetchone()
    if row is None:
        raise ValueError('工作区不存在')
    return dict(row)


def _course(conn, name=None, workspace_id=None):
    text = _text(name)
    if not text:
        return None
    row = conn.execute(
        'SELECT id, name, workspace_id, workspace_id AS ws FROM courses'
        ' WHERE name=? AND (? IS NULL OR workspace_id=?)',
        (text, workspace_id, workspace_id),
    ).fetchone()
    if row is None:
        raise ValueError('课程不存在')
    return dict(row)


def _course_by_id(conn, course_id):
    if course_id is None:
        return None
    row = conn.execute(
        'SELECT id, name, workspace_id, workspace_id AS ws FROM courses WHERE id=?',
        (int(course_id),),
    ).fetchone()
    return dict(row) if row is not None else None


def _kp(conn, kp_id) -> dict:
    number = _int_id(kp_id, 'kp_id')
    row = conn.execute(
        'SELECT k.id, k.course_id, k.name, k.deleted_at, c.workspace_id AS ws'
        ' FROM kp k LEFT JOIN courses c ON c.id=k.course_id WHERE k.id=?',
        (number,),
    ).fetchone()
    if row is None or _text(row['deleted_at']):
        raise ValueError('知识点不存在')
    return dict(row)


def _document(conn, document_id) -> dict:
    number = _int_id(document_id, 'document_id')
    row = conn.execute(
        'SELECT d.id, d.workspace_id, d.course_id, d.kp_id, d.status,'
        ' COALESCE(d.workspace_id, c.workspace_id) AS ws FROM knowledge_documents d'
        ' LEFT JOIN courses c ON c.id=d.course_id WHERE d.id=?',
        (number,),
    ).fetchone()
    if row is None or row['status'] == 'deleted':
        raise ValueError('文档不存在')
    return dict(row)


def _source_row(conn, source_id) -> dict:
    number = _int_id(source_id, 'source_id')
    row = conn.execute(
        'SELECT f.id, f.course_id, f.path, c.workspace_id AS ws FROM source_files f'
        ' LEFT JOIN courses c ON c.id=f.course_id WHERE f.id=?',
        (number,),
    ).fetchone()
    if row is None:
        raise ValueError('材料不存在')
    return dict(row)


def _session_row(conn, session_id) -> dict:
    number = _int_id(session_id, 'session_id')
    row = conn.execute(
        'SELECT s.*, w.key AS workspace_key FROM learning_sessions s'
        ' LEFT JOIN workspaces w ON w.id=s.workspace_id WHERE s.id=?',
        (number,),
    ).fetchone()
    if row is None:
        raise ValueError('学习会话不存在')
    return dict(row)


def _resolve_links(
    conn, course=None, kp_id=None, document_id=None, source_id=None, workspace_id=None
):
    """Validate optional event links and derive the missing ones.

    document -> kp -> course and kp -> course are inferred so callers that only
    know a document id (material reader) still produce a course scoped event.
    Explicit combinations must agree, otherwise the event lies about what was
    studied.
    """
    course_row = _course(conn, course, workspace_id)
    kp_row = _kp(conn, kp_id) if kp_id is not None else None
    document_row = _document(conn, document_id) if document_id is not None else None
    source_row = _source_row(conn, source_id) if source_id is not None else None
    if kp_row is None and document_row is not None and document_row['kp_id'] is not None:
        kp_row = _kp(conn, document_row['kp_id'])
    if course_row is None:
        if kp_row is not None:
            course_row = _course_by_id(conn, kp_row['course_id'])
        elif document_row is not None:
            course_row = _course_by_id(conn, document_row['course_id'])
    if kp_row is not None and course_row is not None and kp_row['course_id'] != course_row['id']:
        raise ValueError('知识点不属于该课程')
    if (
        document_row is not None
        and course_row is not None
        and document_row['course_id'] is not None
        and document_row['course_id'] != course_row['id']
    ):
        raise ValueError('文档不属于该课程')
    if (
        source_row is not None
        and course_row is not None
        and source_row['course_id'] is not None
        and source_row['course_id'] != course_row['id']
    ):
        raise ValueError('材料不属于该课程')
    return course_row, kp_row, document_row, source_row


# Learning lookups expose the owning workspace id under this alias.
_WORKSPACE_COLUMN = 'ws'


def claim_workspace(workspace_id, label, *rows) -> None:
    """Reject links that resolve into a different workspace than the event.

    Rows without an owner (a course-less note, a material outside any course)
    are allowed: they are not bound to another workspace. Rows that do carry an
    owner must agree with the event workspace, otherwise the same event would
    surface another workspace's knowledge point in this workspace's timeline.
    """
    wanted = None if workspace_id is None else int(workspace_id)
    for row in rows:
        if row is None:
            continue
        owner = row.get(_WORKSPACE_COLUMN)
        if owner is None and 'workspace_key' in row:
            owner = row.get('workspace_id')
        elif owner is None and 'workspace_id' in row:
            owner = row.get('workspace_id')
        if owner is not None and wanted is not None and int(owner) != wanted:
            raise ValueError(label + '不属于该工作区')


def _event_row(row) -> dict:
    item = dict(row)
    raw = item.pop('payload_json', '{}')
    try:
        parsed = json.loads(raw) if raw else {}
    except (TypeError, json.JSONDecodeError):
        parsed = raw
    item['payload'] = parsed if isinstance(parsed, dict) else {'value': parsed}
    return item


def _payload_preview(raw):
    text = raw or ''
    try:
        parsed = json.loads(text) if text else {}
    except (TypeError, json.JSONDecodeError):
        parsed = text
    if isinstance(parsed, str):
        preview = parsed
    else:
        preview = json.dumps(parsed, ensure_ascii=False, sort_keys=True)
    if len(preview) > PAYLOAD_PREVIEW:
        return preview[:PAYLOAD_PREVIEW] + '…', True
    return preview, False


def _find_event(conn, key):
    row = conn.execute(
        'SELECT * FROM learning_events WHERE idempotency_key=?', (key,)
    ).fetchone()
    return _event_row(row) if row is not None else None


def record_event(
    kind,
    *,
    workspace=None,
    workspace_key=None,
    course=None,
    kp_id=None,
    document_id=None,
    source_id=None,
    session_id=None,
    payload=None,
    duration_seconds=0,
    occurred_at='',
    idempotency_key='',
    source='',
):
    """Append one learning event; returns a (event, duplicated) tuple.

    workspace is the historical keyword used by review.py while workspace_key
    is the HTTP spelling; both are accepted. source is a caller tag stored in
    payload["source"] when the payload does not set it.
    """
    normalized_kind = _choice(kind, EVENT_KINDS, '事件类型不合法')
    stamp = _timestamp(occurred_at)
    duration = _duration(duration_seconds)
    key = _idempotency(idempotency_key)
    encoded_payload = _payload(payload, source)
    wanted_workspace = workspace_key if workspace_key is not None else workspace
    conn = connect()
    try:
        workspace_row = _workspace(conn, wanted_workspace)
        workspace_id = workspace_row['id']
        session_row = _session_row(conn, session_id) if session_id is not None else None
        if session_row is not None:
            claim_workspace(workspace_id, '学习会话', session_row)
        course_row, kp_row, document_row, source_row = _resolve_links(
            conn,
            course=course,
            kp_id=kp_id,
            document_id=document_id,
            source_id=source_id,
            workspace_id=workspace_id,
        )
        claim_workspace(workspace_id, '知识点', kp_row)
        claim_workspace(workspace_id, '文档', document_row)
        claim_workspace(workspace_id, '材料', source_row)
        if key:
            existing = _find_event(conn, key)
            if existing is not None:
                return existing, True
        try:
            with conn:
                cursor = conn.execute(
                    "INSERT INTO learning_events("
                    " session_id, workspace_id, course_id, kp_id, document_id, source_id,"
                    " kind, payload_json, duration_seconds, occurred_at, idempotency_key"
                    ") VALUES(?,?,?,?,?,?,?,?,?,COALESCE(NULLIF(?,''),datetime('now','localtime')),?)",
                    (
                        session_row['id'] if session_row else None,
                        workspace_row['id'],
                        course_row['id'] if course_row else None,
                        kp_row['id'] if kp_row else None,
                        document_row['id'] if document_row else None,
                        source_row['id'] if source_row else None,
                        normalized_kind,
                        encoded_payload,
                        duration,
                        stamp,
                        key,
                    ),
                )
                event_id = cursor.lastrowid
        except sqlite3.IntegrityError as exc:
            if key:
                existing = _find_event(conn, key)
                if existing is not None:
                    return existing, True
            raise ValueError('学习事件写入失败（幂等键冲突）') from exc
        row = conn.execute(
            'SELECT * FROM learning_events WHERE id=?', (event_id,)
        ).fetchone()
        return _event_row(row), False
    finally:
        conn.close()


def start_session(
    kind='study',
    *,
    workspace=None,
    workspace_key=None,
    course=None,
    source='manual',
    idempotency_key='',
):
    """Open one learning session; returns a (session, duplicated) tuple."""
    normalized_kind = _choice(kind, SESSION_KINDS, '会话类型不合法')
    key = _idempotency(idempotency_key)
    label = _text(source)[:MAX_SOURCE] or 'manual'
    wanted_workspace = workspace_key if workspace_key is not None else workspace
    conn = connect()
    try:
        workspace_row = _workspace(conn, wanted_workspace)
        course_row = _course(conn, course)
        if key:
            row = conn.execute(
                'SELECT * FROM learning_sessions WHERE idempotency_key=?', (key,)
            ).fetchone()
            if row is not None:
                return dict(row), True
        try:
            with conn:
                cursor = conn.execute(
                    'INSERT INTO learning_sessions('
                    ' workspace_id, course_id, kind, source, idempotency_key'
                    ') VALUES(?,?,?,?,?)',
                    (
                        workspace_row['id'],
                        course_row['id'] if course_row else None,
                        normalized_kind,
                        label,
                        key,
                    ),
                )
                session_id = cursor.lastrowid
        except sqlite3.IntegrityError as exc:
            if key:
                row = conn.execute(
                    'SELECT * FROM learning_sessions WHERE idempotency_key=?', (key,)
                ).fetchone()
                if row is not None:
                    return dict(row), True
            raise ValueError('学习会话写入失败（幂等键冲突）') from exc
        row = conn.execute(
            'SELECT * FROM learning_sessions WHERE id=?', (session_id,)
        ).fetchone()
        return dict(row), False
    finally:
        conn.close()


def end_session(session_id, *, ended_at='') -> dict:
    """Close a session; ending an already closed session is idempotent."""
    number = _int_id(session_id, 'session_id')
    stamp = _timestamp(ended_at)
    conn = connect()
    try:
        row = conn.execute(
            'SELECT * FROM learning_sessions WHERE id=?', (number,)
        ).fetchone()
        if row is None:
            raise ValueError('学习会话不存在')
        if _text(row['ended_at']):
            return dict(row)
        with conn:
            conn.execute(
                "UPDATE learning_sessions"
                " SET ended_at=COALESCE(NULLIF(?,''),datetime('now','localtime'))"
                " WHERE id=? AND ended_at=''",
                (stamp, number),
            )
        row = conn.execute(
            'SELECT * FROM learning_sessions WHERE id=?', (number,)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def _period_stats(conn, workspace_id, offset) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS events,"
        " COALESCE(SUM(duration_seconds), 0) AS seconds,"
        " COALESCE(SUM(kind='note_saved'), 0) AS notes,"
        " COALESCE(SUM(kind='candidate_confirmed'), 0) AS candidates_confirmed,"
        " COALESCE(SUM(kind='review_item'), 0) AS reviews"
        " FROM learning_events"
        " WHERE workspace_id=?"
        " AND occurred_at >= date('now','localtime',?)"
        " AND occurred_at < date('now','localtime','+1 day')",
        (workspace_id, offset),
    ).fetchone()
    return {
        'duration_minutes': int(round((row['seconds'] or 0) / 60)),
        'events': int(row['events']),
        'notes': int(row['notes']),
        'candidates_confirmed': int(row['candidates_confirmed']),
        'reviews': int(row['reviews']),
    }


def _streak_days(conn, workspace_id) -> int:
    rows = conn.execute(
        "SELECT DISTINCT substr(occurred_at, 1, 10) AS day FROM learning_events"
        " WHERE workspace_id=? AND occurred_at != ''",
        (workspace_id,),
    ).fetchall()
    days = set()
    for row in rows:
        try:
            days.add(date.fromisoformat(row['day']))
        except (TypeError, ValueError):
            continue
    if not days:
        return 0
    today_text = conn.execute("SELECT date('now','localtime')").fetchone()[0]
    cursor = date.fromisoformat(today_text)
    if cursor not in days:
        cursor -= timedelta(days=1)
    streak = 0
    while cursor in days:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def _recent_materials(conn, workspace_id, limit) -> list:
    rows = conn.execute(
        "SELECT e.source_id AS source_id, s.path AS path,"
        " MAX(e.occurred_at) AS last_event_at,"
        " (SELECT x.kind FROM learning_events x"
        "  WHERE x.source_id=e.source_id AND x.workspace_id=e.workspace_id"
        "  ORDER BY x.occurred_at DESC, x.id DESC LIMIT 1) AS kind"
        " FROM learning_events e"
        " JOIN source_files s ON s.id=e.source_id"
        " WHERE e.workspace_id=? AND e.source_id IS NOT NULL"
        " AND e.kind IN ('open_material','read','excerpt')"
        " GROUP BY e.source_id"
        " ORDER BY last_event_at DESC, e.source_id DESC LIMIT ?",
        (workspace_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def _relative_source_path(path) -> str:
    """Keep only the workspace-relative tail of a stored material path."""
    text = _text(path).replace('\\', '/')
    if not text:
        return ''
    parts = [part for part in text.split('/') if part]
    if len(parts) <= 2:
        return '/'.join(parts)
    return '/'.join(parts[-2:])


def _timeline_groups(conn, workspace_id, range_value):
    if range_value == 'week':
        where = " AND e.occurred_at >= date('now','localtime','-6 days')"
    elif range_value == 'today':
        where = " AND e.occurred_at >= date('now','localtime')"
    else:
        where = ''
    rows = conn.execute(
        "SELECT substr(e.occurred_at, 1, 10) AS date,"
        " COALESCE(c.name, '') AS course,"
        " COUNT(*) AS events,"
        " COALESCE(SUM(e.duration_seconds), 0) AS seconds"
        " FROM learning_events e"
        " LEFT JOIN courses c ON c.id=e.course_id"
        f" WHERE e.workspace_id=?{where}"
        " GROUP BY date, course ORDER BY date DESC, course ASC",
        (workspace_id,),
    ).fetchall()
    return [
        {
            'date': row['date'],
            'minutes': int(round((row['seconds'] or 0) / 60)),
            'events': int(row['events']),
            'course': row['course'],
        }
        for row in rows
    ]


def summary(range='today', *, workspace_key=None) -> dict:
    """Dashboard aggregate: fixed today/week cards plus a range-limited feed."""
    range_value = _choice(range, SUMMARY_RANGES, '统计范围不合法（today/week/all）')
    conn = connect()
    try:
        workspace_row = _workspace(conn, workspace_key)
        return {
            'today': _period_stats(conn, workspace_row['id'], '-0 days'),
            'week': _period_stats(conn, workspace_row['id'], '-6 days'),
            'streak_days': _streak_days(conn, workspace_row['id']),
            'recent_materials': _recent_materials(
                conn, workspace_row['id'], RECENT_MATERIAL_LIMIT
            ),
            'timeline': _timeline_groups(conn, workspace_row['id'], range_value),
        }
    finally:
        conn.close()


def recent_materials(limit=RECENT_MATERIAL_LIMIT, *, workspace_key=None) -> list:
    count = _clamp(limit, 1, MAX_RECENT_MATERIALS, RECENT_MATERIAL_LIMIT)
    conn = connect()
    try:
        workspace_row = _workspace(conn, workspace_key)
        return _recent_materials(conn, workspace_row['id'], count)
    finally:
        conn.close()


def timeline(*, course=None, limit=TIMELINE_LIMIT, workspace_key=None) -> list:
    """Newest-first event feed; payload is truncated to 200 characters."""
    count = _clamp(limit, 1, MAX_TIMELINE_LIMIT, TIMELINE_LIMIT)
    conn = connect()
    try:
        workspace_row = _workspace(conn, workspace_key)
        course_row = _course(conn, course, workspace_row['id'])
        where = 'e.workspace_id=?'
        args = [workspace_row['id']]
        if course_row is not None:
            where += ' AND e.course_id=?'
            args.append(course_row['id'])
        args.append(count)
        rows = conn.execute(
            "SELECT e.id, e.session_id, e.workspace_id, e.course_id, e.kp_id,"
            " e.document_id, e.source_id, e.kind, e.payload_json,"
            " e.duration_seconds, e.occurred_at, e.created_at,"
            " COALESCE(c.name, '') AS course,"
            " COALESCE(k.name, '') AS kp_name,"
            " COALESCE(d.title, '') AS document_title,"
            " COALESCE(s.path, '') AS source_path"
            " FROM learning_events e"
            " LEFT JOIN courses c ON c.id=e.course_id"
            " LEFT JOIN kp k ON k.id=e.kp_id"
            " LEFT JOIN knowledge_documents d ON d.id=e.document_id"
            " LEFT JOIN source_files s ON s.id=e.source_id"
            f" WHERE {where}"
            " ORDER BY e.occurred_at DESC, e.id DESC LIMIT ?",
            args,
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            preview, truncated = _payload_preview(item.pop('payload_json', '{}'))
            item['payload'] = preview
            item['payload_truncated'] = truncated
            item['source_path_rel'] = _relative_source_path(item.get('source_path'))
            item['source_path'] = item['source_path_rel']
            items.append(item)
        return items
    finally:
        conn.close()
