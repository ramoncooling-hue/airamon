"""SQLite FTS5 index with a deterministic LIKE fallback.

FTS5 is optional in embedded SQLite builds (see db.sqlite_capabilities). When
the running build lacks FTS5 -- or the virtual table is missing/unreadable --
queries fall back to LIKE scans over the base tables, and rebuild() becomes a
no-op instead of failing startup (O-DATA-4).
"""

from __future__ import annotations

import logging
import re
import sqlite3

from . import db as _db
from .db import connect

log = logging.getLogger(__name__)

# O-REL-4: FTS failures are classified instead of swallowed. The state below is
# process-local diagnostics; db.sqlite_capabilities() keeps the static probe.
FTS_FAILURE_KINDS = ('missing_module', 'corrupt_index', 'sql_error')
_FTS_STATE = {'kind': '', 'stage': '', 'detail': ''}


def classify_fts_error(exc: BaseException) -> str:
    """Classify one sqlite failure from the FTS index.

    missing_module: the SQLite build/table has no FTS5.
    corrupt_index: the index or database file is damaged.
    sql_error: anything else (bad query, locked database, schema mismatch).
    """
    text = str(exc).lower()
    if 'no such module' in text or 'no such table: search_fts' in text:
        return 'missing_module'
    if 'malformed' in text or 'corrupt' in text or 'disk image' in text:
        return 'corrupt_index'
    return 'sql_error'


def _record_fts_failure(stage: str, exc: BaseException) -> str:
    kind = classify_fts_error(exc)
    _FTS_STATE.update({'kind': kind, 'stage': stage, 'detail': str(exc)[:240]})
    message = '[fts:%s][%s] %s' % (kind, stage, exc)
    if kind == 'missing_module':
        log.warning(message)
    else:
        # Corrupt index / SQL errors keep the traceback for diagnosis.
        log.exception(message)
    return kind


def fts_status() -> dict:
    """Diagnostics for /api/health or maintenance commands (no new endpoint)."""
    capability = bool(_db.sqlite_capabilities().get('fts5'))
    return {'fts5': capability,
            'degraded': bool(not capability or _FTS_STATE['kind']),
            'last_failure_kind': _FTS_STATE['kind'],
            'last_failure_stage': _FTS_STATE['stage'],
            'last_failure_detail': _FTS_STATE['detail']}


def reset_fts_status() -> None:
    """Test/maintenance hook: forget the last failure classification."""
    _FTS_STATE.update({'kind': '', 'stage': '', 'detail': ''})


# Event ownership comes from the event, including events without a course.
_JOINS = """LEFT JOIN courses co ON search_fts.kind != 'event'
                AND co.name=search_fts.course_name
            LEFT JOIN events ev ON search_fts.kind='event'
                AND ev.id=CAST(search_fts.ref_id AS INTEGER)
            LEFT JOIN workspaces ws ON ws.id=CASE WHEN search_fts.kind='event'
                THEN ev.workspace_id ELSE co.workspace_id END"""

# Same six sources as the FTS index, exposed as a UNION for the LIKE fallback.
DEV_SOURCE_KIND = 'development_doc'

# 011's triggers are checksum-frozen historical migrations: a development_doc
# row inserted after rebuild() can still reach search_fts. Hide those rows at
# read time until B5/030 ships a migration that filters at trigger level;
# rebuild() also stops indexing them, so the stale rows are only a small
# hidden residue.
_DEV_FTS_FILTER = """AND NOT EXISTS (
                SELECT 1 FROM source_chunks dev_sc
                  JOIN source_files dev_sf ON dev_sf.id=dev_sc.source_id
                 WHERE search_fts.kind='source_chunk'
                   AND dev_sc.id=CAST(search_fts.ref_id AS INTEGER)
                   AND dev_sf.source_kind='development_doc')
            AND NOT EXISTS (
                SELECT 1 FROM evidence dev_e
                  JOIN source_files dev_sf ON dev_sf.id=dev_e.source_id
                 WHERE search_fts.kind='evidence'
                   AND dev_e.id=CAST(search_fts.ref_id AS INTEGER)
                   AND dev_sf.source_kind='development_doc')
            AND NOT EXISTS (
                SELECT 1 FROM kp dev_k
                  JOIN source_files dev_sf ON dev_sf.path=dev_k.source
                 WHERE search_fts.kind='kp'
                   AND dev_k.id=CAST(search_fts.ref_id AS INTEGER)
                   AND dev_sf.source_kind='development_doc')"""

_KP_DEV_FILTER = """NOT EXISTS (
            SELECT 1 FROM source_files dev_sf
             WHERE dev_sf.source_kind='development_doc' AND dev_sf.path=kp.source)"""

# 026 soft-deleted nodes are hidden everywhere (DATABASE.md contract). 011's
# triggers are frozen historical migrations, so a soft delete leaves the kp row
# inside search_fts; filter at read time and stop indexing it in rebuild().
_DELETED_KP_FTS_FILTER = """AND NOT EXISTS (
                SELECT 1 FROM kp deleted_kp
                 WHERE search_fts.kind='kp'
                   AND deleted_kp.id=CAST(search_fts.ref_id AS INTEGER)
                   AND COALESCE(deleted_kp.deleted_at,'')!='')"""


def _like_sources(include_dev: bool = False) -> tuple:
    """Base-table UNION for the LIKE fallback; development docs are hidden."""
    chunk_dev = '' if include_dev else " AND sf.source_kind!='development_doc'"
    kp_dev = "WHERE COALESCE(kp.deleted_at,'')=''"
    if not include_dev:
        kp_dev += f' AND {_KP_DEV_FILTER}'
    return (
        f"""SELECT 'kp' AS kind, CAST(kp.id AS TEXT) AS ref_id, co.name AS course_name,
              kp.name AS title,
              coalesce(kp.gist,'')||' '||coalesce(kp.detail,'')||' '||
              coalesce(kp.trap,'')||' '||coalesce(kp.related,'') AS body,
              COALESCE(ws.key,'studypal') AS ws_key
         FROM kp JOIN courses co ON co.id=kp.course_id
         LEFT JOIN workspaces ws ON ws.id=co.workspace_id
         {kp_dev}""",
        f"""SELECT 'source_chunk', CAST(sc.id AS TEXT), co.name, sf.path, sc.content,
              COALESCE(ws.key,'studypal')
         FROM source_chunks sc JOIN source_files sf ON sf.id=sc.source_id
         JOIN courses co ON co.id=sf.course_id
         LEFT JOIN workspaces ws ON ws.id=co.workspace_id
        WHERE sf.status='indexed' AND sc.stale=0{chunk_dev}""",
        f"""SELECT 'evidence', CAST(e.id AS TEXT), co.name,
              sf.path||' '||coalesce(e.locator_type,''), e.content,
              COALESCE(ws.key,'studypal')
         FROM evidence e JOIN source_files sf ON sf.id=e.source_id
         JOIN courses co ON co.id=sf.course_id
         LEFT JOIN workspaces ws ON ws.id=co.workspace_id
        WHERE sf.status='indexed' AND e.stale=0{chunk_dev}""",
        """SELECT 'event', CAST(ev.id AS TEXT), ev.course_name, ev.title,
              coalesce(ev.note,'')||' '||coalesce(ev.category,'')||' '||coalesce(ev.date,''),
              COALESCE(ws.key,'studypal')
         FROM events ev LEFT JOIN workspaces ws ON ws.id=ev.workspace_id""",
        """SELECT 'notice', CAST(n.id AS TEXT), n.course_name, n.title,
              coalesce(n.body,'')||' '||coalesce(n.source,'')||' '||coalesce(n.date,''),
              COALESCE(ws.key,'studypal')
         FROM notices n LEFT JOIN courses co ON co.name=n.course_name
         LEFT JOIN workspaces ws ON ws.id=co.workspace_id""",
        """SELECT 'progress', CAST(p.id AS TEXT), co.name, p.topic,
              coalesce(p.homework,'')||' '||coalesce(p.note,'')||' '||coalesce(p.due_at,''),
              COALESCE(ws.key,'studypal')
         FROM progress p JOIN courses co ON co.id=p.course_id
         LEFT JOIN workspaces ws ON ws.id=co.workspace_id""",
    )


def _terms(query: str) -> list[str]:
    text = ' '.join(str(query or '').split()).strip()
    words = re.findall(r'[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}', text)
    if not words and text:
        words = [text[:80]]
    return list(dict.fromkeys(words[:12]))


def _like_terms_sql(terms: list[str], prefix: str = '') -> tuple[str, list]:
    clauses = ' OR '.join(
        f'({prefix}title LIKE ? OR {prefix}body LIKE ?)' for _ in terms)
    args: list = []
    for term in terms:
        args.extend([f'%{term}%', f'%{term}%'])
    return clauses, args


def dedupe_index_rows(rows: list[dict], limit: int) -> list[dict]:
    """Keep the first (most relevant) row for each (kind, ref_id) pair.

    FTS can contain duplicate postings after a partial rebuild or a manually
    restored database, and the LIKE fallbacks can match the same base row from
    several terms. Dedupe before the caller-visible LIMIT so duplicates never
    consume result slots; order is preserved so the best-ranked hit wins.
    """
    seen: set[tuple[str, str]] = set()
    unique: list[dict] = []
    for row in rows:
        key = (str(row.get('kind') or ''), str(row.get('ref_id') or ''))
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
        if len(unique) >= limit:
            break
    return unique


def _base_like_search(terms: list[str], course: str | None, limit: int,
                      workspace: str | None, material_only: bool,
                      include_dev: bool = False) -> list[dict]:
    """Scan the base tables when no usable search_fts table exists."""
    clauses, args = _like_terms_sql(terms)
    sql = ('SELECT kind, ref_id, course_name, title, body, 0 AS rank FROM ('
           + ' UNION ALL '.join(_like_sources(include_dev)) + f') WHERE ({clauses})')
    if course:
        sql += ' AND course_name=?'
        args.append(course)
    if workspace:
        sql += ' AND ws_key=?'
        args.append(workspace)
    if material_only:
        sql += " AND kind IN ('kp','source_chunk','evidence')"
    # Over-fetch so (kind, ref_id) duplicates never crowd out unique rows;
    # search() performs the bounded dedupe.
    sql += ' LIMIT ?'
    args.append(max(limit * 3, limit))
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def rebuild() -> int:
    if not _db.sqlite_capabilities().get('fts5'):
        # 011 was skipped on this build; LIKE scans remain available.
        return 0
    conn = connect()
    try:
        try:
            conn.execute("DELETE FROM search_fts")
            conn.execute("""INSERT INTO search_fts(kind,ref_id,course_name,title,body)
                           SELECT 'kp',CAST(kp.id AS TEXT),co.name,kp.name,
                                  coalesce(kp.gist,'')||' '||coalesce(kp.detail,'')||' '||
                                  coalesce(kp.trap,'')||' '||coalesce(kp.related,'')
                           FROM kp JOIN courses co ON co.id=kp.course_id
                           WHERE COALESCE(kp.deleted_at,'')=''
                             AND NOT EXISTS (SELECT 1 FROM source_files dev_sf
                                              WHERE dev_sf.source_kind='development_doc'
                                                AND dev_sf.path=kp.source)""")
            # 022: retired (stale) extraction rows are history; never index them.
            conn.execute("""INSERT INTO search_fts(kind,ref_id,course_name,title,body)
                           SELECT 'source_chunk',CAST(sc.id AS TEXT),co.name,sf.path,sc.content
                           FROM source_chunks sc JOIN source_files sf ON sf.id=sc.source_id
                           JOIN courses co ON co.id=sf.course_id
                           WHERE sf.status='indexed' AND sc.stale=0
                             AND sf.source_kind!='development_doc'""")
            # 022: evidence retired by a rescan stays out of full-text search.
            conn.execute("""INSERT INTO search_fts(kind,ref_id,course_name,title,body)
                           SELECT 'evidence',CAST(e.id AS TEXT),co.name,
                                  sf.path||' '||coalesce(e.locator_type,''),e.content
                           FROM evidence e JOIN source_files sf ON sf.id=e.source_id
                           JOIN courses co ON co.id=sf.course_id
                           WHERE sf.status='indexed' AND e.stale=0
                             AND sf.source_kind!='development_doc'""")
            conn.execute("""INSERT INTO search_fts(kind,ref_id,course_name,title,body)
                           SELECT 'event',CAST(id AS TEXT),course_name,title,
                                  coalesce(note,'')||' '||coalesce(category,'')||' '||coalesce(date,'')
                           FROM events""")
            conn.execute("""INSERT INTO search_fts(kind,ref_id,course_name,title,body)
                           SELECT 'notice',CAST(id AS TEXT),course_name,title,
                                  coalesce(body,'')||' '||coalesce(source,'')||' '||coalesce(date,'')
                           FROM notices""")
            conn.execute("""INSERT INTO search_fts(kind,ref_id,course_name,title,body)
                           SELECT 'progress',CAST(p.id AS TEXT),co.name,p.topic,
                                  coalesce(p.homework,'')||' '||coalesce(p.note,'')||' '||coalesce(p.due_at,'')
                           FROM progress p JOIN courses co ON co.id=p.course_id""")
            count = conn.execute("SELECT COUNT(*) FROM search_fts").fetchone()[0]
            conn.commit()
            return int(count)
        except sqlite3.Error as exc:
            # A damaged index must not break startup or rebuild; the LIKE
            # fallback keeps retrieval usable and the failure is classified.
            _record_fts_failure('rebuild', exc)
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            return 0
    finally:
        conn.close()


def search(query: str, course: str | None = None, limit: int = 10,
           workspace: str | None = None, *, material_only: bool = False,
           include_dev: bool = False) -> list[dict]:
    """Search the FTS index; development docs are hidden unless include_dev=True.

    include_dev exists only for debugging the exclusion itself.
    """
    terms = _terms(query)
    if not terms:
        return []
    limit = max(1, min(int(limit), 50))
    fetch_limit = max(limit * 3, limit)
    if not _db.sqlite_capabilities().get('fts5'):
        return dedupe_index_rows(
            _base_like_search(terms, course, fetch_limit, workspace, material_only, include_dev), limit)
    match = ' OR '.join('"' + term.replace('"', ' ') + '"' for term in terms)
    conn = connect()
    try:
        try:
            sql = f"""SELECT search_fts.kind,search_fts.ref_id,search_fts.course_name,
                             search_fts.title,search_fts.body,bm25(search_fts) AS rank
                        FROM search_fts {_JOINS}
                       WHERE search_fts MATCH ?"""
            args: list = [match]
            if course:
                sql += ' AND search_fts.course_name=?'
                args.append(course)
            if workspace:
                sql += " AND COALESCE(ws.key,'studypal')=?"
                args.append(workspace)
            if material_only:
                sql += " AND search_fts.kind IN ('kp','source_chunk','evidence')"
            if not include_dev:
                sql += ' ' + _DEV_FTS_FILTER
            sql += ' ' + _DELETED_KP_FTS_FILTER
            sql += ' ORDER BY rank LIMIT ?'
            args.append(fetch_limit)
            rows = [dict(row) for row in conn.execute(sql, args).fetchall()]
            if rows:
                return dedupe_index_rows(rows, limit)
        except sqlite3.Error as exc:
            _record_fts_failure('match', exc)
        # FTS5 is optional in some embedded SQLite builds. Keep the app useful.
        try:
            clauses, args = _like_terms_sql(terms, 'search_fts.')
            sql = f'''SELECT search_fts.kind,search_fts.ref_id,search_fts.course_name,
                             search_fts.title,search_fts.body,0 AS rank
                        FROM search_fts {_JOINS}
                       WHERE ({clauses})'''
            if course:
                sql += ' AND search_fts.course_name=?'
                args.append(course)
            if workspace:
                sql += " AND COALESCE(ws.key,'studypal')=?"
                args.append(workspace)
            if material_only:
                sql += " AND search_fts.kind IN ('kp','source_chunk','evidence')"
            if not include_dev:
                sql += ' ' + _DEV_FTS_FILTER
            sql += ' ' + _DELETED_KP_FTS_FILTER
            sql += ' LIMIT ?'
            args.append(fetch_limit)
            return dedupe_index_rows(
                [dict(row) for row in conn.execute(sql, args).fetchall()], limit)
        except sqlite3.Error as exc:
            _record_fts_failure('like', exc)
    finally:
        conn.close()
    # The virtual table is missing (011 skipped on an FTS5-less build) or
    # unreadable: scan the base tables directly.
    return dedupe_index_rows(
        _base_like_search(terms, course, fetch_limit, workspace, material_only, include_dev), limit)
