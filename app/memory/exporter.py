"""Markdown vault export (T3.11 / O-KB-6): one .md file per knowledge document.

v0 is one-way: documents become Obsidian-compatible Markdown files under
<data>/exports/knowledge-<stamp>/ (or a caller-provided directory that must stay
inside the data directory). Bodies are emitted verbatim so [[wikilinks]] keep
working as-is; attachments are not copied. Archived documents go to an
archived/ subdirectory and deleted documents are skipped (counted in skipped).
A manifest.json records what was written for auditing; the returned files list
contains only .md paths relative to the export directory.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from . import db

EXPORT_DIRNAME = 'exports'
EXPORT_PREFIX = 'knowledge-'
MANIFEST_NAME = 'manifest.json'
MANIFEST_FORMAT = 'mindock.knowledge.v1'
MAX_FILENAME = 80
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_TRAVERSAL = re.compile(r'\.\.')
_WHITESPACE = re.compile(r'\s+')
_DASHES = re.compile(r'-{2,}')
_PLAIN_SAFE = re.compile(r"^[\w\u4e00-\u9fff][\w\u4e00-\u9fff .()（）\-]*$")
_NUMBER = re.compile(r'^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$')
_RESERVED = {'null', 'true', 'false', 'yes', 'no', 'on', 'off', '~'}


def _yaml_scalar(value) -> str:
    """Render a frontmatter scalar, double-quoting values YAML would misread."""
    text = '' if value is None else str(value)
    if not text:
        return '""'
    if _PLAIN_SAFE.match(text) and not _NUMBER.match(text) and text.lower() not in _RESERVED:
        return text
    escaped = (text.replace('\\', '\\\\').replace('"', '\\"')
               .replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t'))
    return '"' + escaped + '"'


def _safe_filename(title) -> str:
    """Cross-platform-safe markdown stem that keeps Chinese characters."""
    name = _ILLEGAL.sub('-', str(title or ''))
    name = _TRAVERSAL.sub('-', name)
    name = _WHITESPACE.sub(' ', name).strip(' .-')
    name = _DASHES.sub('-', name).strip(' .-')
    if not name or name in {'.', '..'}:
        name = 'untitled'
    return name[:MAX_FILENAME].rstrip(' .-') or 'untitled'


def _check_destination(dest_dir):
    """Resolve the destination parent and reject paths outside the data dir."""
    data_root = Path(db.DATA_DIR).resolve()
    if dest_dir in (None, ''):
        candidate = data_root / EXPORT_DIRNAME
    else:
        candidate = Path(str(dest_dir)).expanduser()
        if not candidate.is_absolute():
            candidate = data_root / candidate
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError('导出目录不可用') from exc
    try:
        inside = os.path.commonpath([str(resolved), str(data_root)]) == str(data_root)
    except ValueError:
        inside = False
    if not inside:
        raise ValueError('导出目录必须位于数据目录内')
    return resolved, data_root


def _create_export_dir(parent: Path, data_root: Path) -> Path:
    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError('导出目录不可用') from exc
    if not parent.is_dir():
        raise ValueError('导出目录不可用')
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    base = f'{EXPORT_PREFIX}{stamp}'
    for index in range(1, 1000):
        candidate = parent / (base if index == 1 else f'{base}-{index}')
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:
            continue
        except OSError as exc:
            raise ValueError('导出目录不可用') from exc
        resolved = candidate.resolve()
        if os.path.commonpath([str(resolved), str(data_root)]) != str(data_root):
            raise ValueError('导出目录必须位于数据目录内')
        return candidate
    raise ValueError('导出目录不可用')


def _workspace(conn, workspace_key) -> dict:
    key = ' '.join(str(workspace_key or 'studypal').split()).strip() or 'studypal'
    row = conn.execute(
        "SELECT id,key FROM workspaces WHERE key=? AND status='active'", (key,)).fetchone()
    if not row:
        raise ValueError('工作区不存在')
    return dict(row)


def _document_rows(conn, workspace_id: int):
    rows = conn.execute(
        '''SELECT d.id,d.title,d.body_md,d.kind,d.status,d.version,d.kp_id,d.course_id,
                  d.created_at,d.updated_at,c.name AS course_name
           FROM knowledge_documents d LEFT JOIN courses c ON c.id=d.course_id
           WHERE d.workspace_id=? AND d.status IN ('active','archived')
           ORDER BY d.id''', (workspace_id,)).fetchall()
    documents = []
    for row in rows:
        item = dict(row)
        item['tags'] = [tag['name'] for tag in conn.execute(
            '''SELECT t.name FROM entity_tags et JOIN tags t ON t.id=et.tag_id
               WHERE et.entity_type='document' AND et.entity_id=? ORDER BY t.name''',
            (row['id'],)).fetchall()]
        documents.append(item)
    skipped = int(conn.execute(
        "SELECT COUNT(*) FROM knowledge_documents WHERE workspace_id=? AND status='deleted'",
        (workspace_id,)).fetchone()[0])
    return documents, skipped


def _frontmatter(document: dict, workspace: dict) -> str:
    tags = ', '.join(_yaml_scalar(tag) for tag in document['tags'])
    kp_id = 'null' if document['kp_id'] is None else str(int(document['kp_id']))
    lines = [
        '---',
        f'title: {_yaml_scalar(document["title"])}',
        f'workspace: {_yaml_scalar(workspace["key"])}',
        f'course: {_yaml_scalar(document.get("course_name") or "")}',
        f'tags: [{tags}]',
        f'kind: {_yaml_scalar(document["kind"])}',
        f'kp_id: {kp_id}',
        f'created: {_yaml_scalar(document["created_at"])}',
        f'updated: {_yaml_scalar(document["updated_at"])}',
        '---',
    ]
    return '\n'.join(lines)


def _unique_name(used: set, base: str) -> str:
    candidate = f'{base}.md'
    index = 2
    while candidate.casefold() in used:
        suffix = f'-{index}'
        stem = base[:max(1, MAX_FILENAME - len(suffix))].rstrip(' .-') or 'untitled'
        candidate = f'{stem}{suffix}.md'
        index += 1
    used.add(candidate.casefold())
    return candidate


def _write_text(path: Path, text: str) -> None:
    try:
        path.write_text(text, encoding='utf-8')
        path.chmod(0o600)
    except OSError as exc:
        raise ValueError('导出文件写入失败') from exc


def export_knowledge(workspace_key: str = 'studypal', dest_dir=None) -> dict:
    """Export active/archived documents of one workspace into a Markdown vault."""
    parent, data_root = _check_destination(dest_dir)
    conn = db.connect()
    try:
        workspace = _workspace(conn, workspace_key)
        documents, skipped = _document_rows(conn, workspace['id'])
    finally:
        conn.close()
    export_dir = _create_export_dir(parent, data_root)
    active_names: set = set()
    archived_names: set = set()
    files = []
    entries = []
    for document in documents:
        archived = document['status'] == 'archived'
        directory = export_dir / 'archived' if archived else export_dir
        if archived:
            try:
                directory.mkdir(mode=0o700, exist_ok=True)
            except OSError as exc:
                raise ValueError('导出目录不可用') from exc
        base = _safe_filename(document['title'])
        name = _unique_name(archived_names if archived else active_names, base)
        target = directory / name
        _write_text(target, _frontmatter(document, workspace) + '\n' + document['body_md'])
        relative = target.relative_to(export_dir).as_posix()
        files.append(relative)
        entries.append({
            'file': relative,
            'document_id': int(document['id']),
            'title': document['title'],
            'kind': document['kind'],
            'status': document['status'],
            'version': int(document['version']),
            'updated_at': document['updated_at'],
        })
    manifest = {
        'format': MANIFEST_FORMAT,
        'workspace': workspace['key'],
        'exported_at': datetime.now().isoformat(timespec='seconds'),
        'documents': entries,
        'skipped': skipped,
    }
    _write_text(export_dir / MANIFEST_NAME,
                json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    return {'ok': True, 'dir': str(export_dir), 'files': files, 'skipped': skipped}
