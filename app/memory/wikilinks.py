"""Wiki links (dual links) v1 for editable documents (T2.23 backend).

Parsing is pure: [[target]] / [[target|label]] inside a Markdown body, ignoring
fenced and inline code. Resolution and backlinks are computed on the fly from
knowledge_documents; a workspace scan is accepted at the <=5000 document scale
(B2). B5/029 introduces a persisted links table with audit + soft delete and
replaces these scans, so callers must treat this module as a query helper, not
a source of truth.
"""

from __future__ import annotations

import re

from .db import connect

MAX_TARGET = 200
_CONTEXT_WIDTH = 60

# Fenced blocks first, then inline code; masked by spaces so match offsets stay
# valid against the original body.
_FENCE = re.compile(r'(?ms)^[ \t]*(?:```|~~~)[^\n]*\n.*?(?:^[ \t]*(?:```|~~~)[ \t]*$|\Z)')
_INLINE_CODE = re.compile(r'`[^`\n]*`')
_WIKILINK = re.compile(r'\[\[([^\[\]\n]{1,200})\]\]')
_WHITESPACE = re.compile(r'\s+')


def _masked(body: str) -> str:
    chars = list(body)

    def blank(match) -> None:
        for index in range(match.start(), match.end()):
            if chars[index] != '\n':
                chars[index] = ' '

    for match in _FENCE.finditer(body):
        blank(match)
    for match in _INLINE_CODE.finditer(body):
        blank(match)
    return ''.join(chars)


def parse_wikilinks(body_md) -> list[dict]:
    """Return [{target,label,start,end}] in body order (code spans ignored)."""
    text = str(body_md or '')
    if '[[' not in text:
        return []
    links = []
    for match in _WIKILINK.finditer(_masked(text)):
        inner = match.group(1)
        target, _, label = inner.partition('|')
        target = ' '.join(target.split()).strip()
        label = ' '.join(label.split()).strip() or target
        if not target or len(target) > MAX_TARGET:
            continue
        links.append({'target': target, 'label': label,
                      'start': match.start(), 'end': match.end()})
    return links


def _context(body, link) -> str:
    text = str(body or '')
    start = max(0, int(link['start']) - _CONTEXT_WIDTH)
    end = min(len(text), int(link['end']) + _CONTEXT_WIDTH)
    return _WHITESPACE.sub(' ', text[start:end]).strip()


def resolve_links(conn, workspace_id, targets) -> dict:
    """Resolve [[target]]s by document title, then by kp name.

    Documents with the same title resolve to the most recently updated active
    one (title is deliberately not UNIQUE in 026; the ambiguity is reported by
    the API docs and is accepted for v1). A kp that has no document still counts
    as an existing target and is returned under 'kp_only'.
    """
    wanted: list[str] = []
    for target in targets:
        name = ' '.join(str(target or '').split()).strip()
        if name and name not in wanted:
            wanted.append(name)
    if not wanted:
        return {'resolved': {}, 'kp_only': {}, 'missing': []}
    marks = ','.join('?' for _ in wanted)
    resolved: dict[str, int] = {}
    rows = conn.execute(
        f'''SELECT id,title FROM knowledge_documents
             WHERE workspace_id IS ? AND status='active' AND title IN ({marks})
             ORDER BY updated_at DESC, id DESC''',
        [workspace_id, *wanted]).fetchall()
    for row in rows:
        if row['title'] not in resolved:
            resolved[str(row['title'])] = int(row['id'])
    kp_only: dict[str, int] = {}
    missing_names = [name for name in wanted if name not in resolved]
    if missing_names:
        marks = ','.join('?' for _ in missing_names)
        rows = conn.execute(
            f'''SELECT kp.id AS kp_id, kp.name AS name, d.id AS document_id
                  FROM kp
                  JOIN courses co ON co.id=kp.course_id
                  LEFT JOIN knowledge_documents d ON d.kp_id=kp.id AND d.status='active'
                 WHERE COALESCE(kp.deleted_at,'')=''
                   AND kp.name IN ({marks})
                   AND COALESCE((SELECT w.key FROM workspaces w WHERE w.id=co.workspace_id),
                                'studypal') = (SELECT w.key FROM workspaces w WHERE w.id=?)
                 ORDER BY kp.id''',
            [*missing_names, workspace_id]).fetchall()
        for row in rows:
            name = str(row['name'])
            if row['document_id'] is not None:
                resolved.setdefault(name, int(row['document_id']))
            else:
                kp_only.setdefault(name, int(row['kp_id']))
    missing = [name for name in wanted if name not in resolved and name not in kp_only]
    return {'resolved': resolved, 'kp_only': kp_only, 'missing': missing}


def resolve_titles(conn, workspace_id, titles) -> dict:
    """Resolution payload used by GET /api/documents/lookup."""
    result = resolve_links(conn, workspace_id, titles)
    return {'ids': result['resolved'], 'kp_ids': result['kp_only'],
            'missing': result['missing']}


def lookup_titles(workspace_key, titles) -> dict:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id FROM workspaces WHERE key=? AND status='active'",
            (' '.join(str(workspace_key or 'studypal').split()).strip(),)).fetchone()
        if not row:
            raise ValueError('工作区不存在')
        return resolve_titles(conn, int(row['id']), titles)
    finally:
        conn.close()


def backlinks(document, *, conn=None) -> dict:
    """Return {backlinks, bad_links} for one document, computed from live rows.

    bad_links are the document's own [[...]] targets that match neither an
    active document title nor a kp name; backlinks are the active documents of
    the same workspace that link to this document's title (or its kp name).
    One entry per linking document, newest first.
    """
    own_conn = conn is None
    conn = conn or connect()
    try:
        document_id = int(document['id'])
        workspace_id = document.get('workspace_id')
        title = str(document.get('title') or '')
        body = str(document.get('body_md') or '')
        parsed = parse_wikilinks(body)
        resolution = resolve_links(conn, workspace_id, [link['target'] for link in parsed])
        bad_links = [{'target': target, 'reason': 'missing'}
                     for target in resolution['missing']]
        names = {title} if title else set()
        kp_id = document.get('kp_id')
        if kp_id:
            row = conn.execute('SELECT name FROM kp WHERE id=?', (int(kp_id),)).fetchone()
            if row and row['name']:
                names.add(str(row['name']))
        rows = conn.execute(
            '''SELECT id,title,body_md FROM knowledge_documents
                 WHERE workspace_id IS ? AND status='active' AND id<>?
                 ORDER BY updated_at DESC, id DESC''',
            (workspace_id, document_id)).fetchall()
        incoming = []
        for row in rows:
            for link in parse_wikilinks(row['body_md']):
                if link['target'] in names:
                    incoming.append({'document_id': int(row['id']),
                                     'title': str(row['title'] or ''),
                                     'link_text': link['label'],
                                     'target': link['target'],
                                     'context': _context(row['body_md'], link)})
                    break
        return {'backlinks': incoming, 'bad_links': bad_links}
    finally:
        if own_conn:
            conn.close()
