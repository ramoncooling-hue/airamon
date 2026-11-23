"""WorkPal · Obsidian Wiki 导入模块（vault 解析 + 幂等建图 + 后台任务）。

从 scripts/import_wiki_workpal.py 迁移：把编译后的 wiki/ Markdown 页面当作
规范图谱来源，将显式 [[Wiki 链接]] 物化为 WorkPal 知识点与 related 边。
导入是幂等的；正式图写入仍只经 sources.authorize_formal_write 审计通道
（CLI 走脚本名校验，API 触发时 allow_api=True）。

同时提供：
- vault_candidates：从 obsidian.json 发现本机 Obsidian 仓库；
- start_import/status/reset_job_state：单并发后台导入任务状态机。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import posixpath
import re
import shutil
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from . import db, sources, documents
from .wikilinks import parse_wikilinks


logger = logging.getLogger('mindock.wiki_import')

SCHEMA_VERSION = "cyberpal.knowledge.v1"
DEFAULT_WIKI_ROOT = (Path.home() / "Documents" / "LLMWiki" / "LLM wiki" / "wiki")
DEFAULT_COURSE = "AI工作系统"
WORKPAL_WORKSPACE = "workpal"
MAX_GIST = 1000
MAX_DETAIL = 6000
MAX_EVIDENCE_CHARS = 2600


@dataclass(frozen=True)
class WikiPage:
    path: Path
    relative: str
    title: str
    page_type: str
    tags: tuple[str, ...]
    source_ref: str
    knowledge_status: str
    body: str
    gist: str
    chapter: str
    chapter_key: str
    links: tuple[str, ...]
    digest: str
    uid: str = ''
    aliases: tuple[str, ...] = ()
    parse_warning: bool = False


def _clean_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, flags=re.S)
    if not match:
        return {}, text
    try:
        values = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        # Older compiled pages contain unquoted title punctuation. Preserve the
        # legacy scalar fallback but surface the affected page in the receipt.
        values = {'__parse_warning__': True}
        for line in match.group(1).splitlines():
            item = re.match(r'^([A-Za-z_][\w-]*):\s*(.*)$', line)
            if item and item.group(1) != '__parse_warning__':
                values[item.group(1)] = _clean_scalar(item.group(2))
    if not isinstance(values, dict):
        values = {}
    return values, text[match.end():]


def _tags(value: str) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(dict.fromkeys(str(item).strip() for item in value
                                  if isinstance(item, (str, int, float)) and str(item).strip()))
    value = str(value or '').strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    result = []
    for item in re.split(r",|、", value):
        item = _clean_scalar(item).strip()
        if item:
            result.append(item)
    return tuple(dict.fromkeys(result))


def _section(body: str, heading: str) -> str:
    match = re.search(
        rf"^##\s+{re.escape(heading)}\s*$([\s\S]*?)(?=^##\s+|\Z)",
        body,
        flags=re.M,
    )
    return match.group(1).strip() if match else ""


def _plain_excerpt(text: str, limit: int) -> str:
    text = re.sub(r"```[\s\S]*?```", " ", text)
    text = re.sub(r"!\[[^]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*_>`#|]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _title(frontmatter: dict[str, str], body: str, path: Path) -> str:
    title = str(frontmatter.get("title") or '').strip()
    if title:
        return title[:180]
    heading = re.search(r"^#\s+(.+?)\s*$", body, flags=re.M)
    return (heading.group(1).strip() if heading else path.stem)[:180]


def _stable_uid(frontmatter: dict[str, object]) -> str:
    """Read an optional source identity without inventing one from content."""
    values = []
    for key in ('uid', 'id'):
        value = frontmatter.get(key)
        if value is None or value == '':
            continue
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            raise ValueError(f'Wiki 身份 {key} 必须是字符串或整数')
        value = str(value).strip()
        if not value or len(value) > 180 or any(ord(c) < 32 for c in value):
            raise ValueError(f'Wiki 身份 {key} 格式无效')
        values.append(value)
    if len(set(values)) > 1:
        raise ValueError('Wiki 身份 id 与 uid 不一致')
    return values[0] if values else ''


def _wiki_links(body: str) -> tuple[str, ...]:
    links = []
    for link in parse_wikilinks(body):
        raw = link['target'].split('#', 1)[0]
        target = raw.strip().replace("\\", "/")
        if target.lower().endswith(".md"):
            target = target[:-3]
        if target:
            links.append(target)
    return tuple(dict.fromkeys(links))


def _chapter_values(relative: str) -> tuple[str, str]:
    parent = Path(relative).parent.as_posix()
    chapter = parent if parent != "." else "Wiki"
    safe = re.sub(r"[^a-z0-9]+", "-", chapter.lower()).strip("-") or "root"
    if not chapter.isascii():
        safe += '-' + hashlib.sha256(chapter.encode('utf-8')).hexdigest()[:12]
    return chapter, f"wiki-{safe}"


def parse_page(path: Path, root: Path) -> WikiPage:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    frontmatter, body = _frontmatter(text)
    relative = path.relative_to(root).as_posix()
    chapter, chapter_key = _chapter_values(relative)
    summary = _section(body, "摘要") or _section(body, "核心观点")
    if not summary:
        summary = next((line for line in body.splitlines() if line.strip()), path.stem)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return WikiPage(
        path=path,
        relative=relative,
        title=_title(frontmatter, body, path),
        page_type=str(frontmatter.get("type") or "reference").strip().lower(),
        tags=_tags(frontmatter.get("tags", "")),
        source_ref=str(frontmatter.get("source") or '').strip(),
        knowledge_status=str(frontmatter.get("knowledge_status") or "pending_review").strip(),
        body=body,
        gist=_plain_excerpt(summary, MAX_GIST),
        chapter=chapter,
        chapter_key=chapter_key,
        links=_wiki_links(body),
        digest=digest,
        uid=_stable_uid(frontmatter),
        aliases=_tags(frontmatter.get('aliases', frontmatter.get('alias', ''))),
        parse_warning=bool(frontmatter.get('__parse_warning__')),
    )


def is_graph_page(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if any(part.startswith("_") for part in relative.parts):
        return False
    if path.name == "INDEX.md" or path.name.endswith("-INDEX.md"):
        return False
    if path.name.startswith("HEALTH_REPORT"):
        return False
    return True


def resolve_wiki_root(value: Path | str) -> Path:
    if not str(value or '').strip():
        raise ValueError('请选择 Wiki 目录')
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f'Wiki 目录不存在：{root}')
    child = root / 'wiki'
    if child.is_dir() and not child.is_symlink():
        return child
    return root


def _is_compiled_wiki(root: Path) -> bool:
    # LLM wiki exports have both a root INDEX and at least one chapter index;
    # a user may legitimately name an ordinary vault folder "wiki".
    if not (root / 'INDEX.md').is_file() or not (root / '_governance').is_dir():
        return False
    if not any(root.glob('HEALTH_REPORT*.md')):
        return False
    return any(root.glob('*/**/*-INDEX.md'))


def _markdown_paths(root: Path):
    # Prune runtime/config trees before traversal; never follow source symlinks.
    excluded = {'node_modules', '__pycache__', 'venv', 'output', 'outputs'}
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if not name.startswith('.')
                         and name.lower() not in excluded
                         and not (Path(directory) / name).is_symlink())
        for name in sorted(files):
            path = Path(directory) / name
            if not name.startswith('.') and path.suffix.lower() == '.md' and not path.is_symlink():
                yield path


def iter_pages(root: Path) -> list[WikiPage]:
    compiled = _is_compiled_wiki(root)
    return [parse_page(path, root) for path in sorted(_markdown_paths(root))
            if not compiled or is_graph_page(path, root)]


def _node_type(page_type: str) -> str:
    return {
        "concept": "concept",
        "guide": "method",
        "reference": "material",
        "overview": "model",
    }.get(page_type, "material")


def _status(value: str) -> str:
    return {
        "current": "confirmed",
        "pending_review": "pending_review",
        "disputed": "pending_review",
        "superseded": "stale",
        "retired": "archived",
    }.get(value, "pending_review")


def _blocks(text: str) -> list[tuple[int, int, str]]:
    lines = text.splitlines()
    rows: list[tuple[int, int, str]] = []
    start = None
    buffer: list[str] = []
    size = 0
    for number, line in enumerate(lines, 1):
        if start is None and not line.strip():
            continue
        if start is None:
            start = number
        buffer.append(line)
        size += len(line) + 1
        if size >= MAX_EVIDENCE_CHARS:
            rows.append((start, number, "\n".join(buffer).strip()))
            start, buffer, size = None, [], 0
    if buffer and start is not None:
        rows.append((start, len(lines), "\n".join(buffer).strip()))
    return [(start, end, text) for start, end, text in rows if text]


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _copy_path(page: WikiPage, copy_root: Path) -> Path:
    _check_destination(copy_root / page.relative)
    destination = (copy_root / page.relative).resolve()
    if not destination.is_relative_to(copy_root.resolve()):
        raise ValueError(f"Wiki 路径越界：{page.relative}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(page.path, destination)
    return destination


def _check_destination(path: Path) -> None:
    data_root = db.DATA_DIR.resolve()
    relative = path.relative_to(db.DATA_DIR)
    current = data_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('Wiki 导入目标不能含符号链接')


def _merge_refs(old: str, new: dict) -> str:
    try:
        refs = json.loads(old or "[]")
    except json.JSONDecodeError:
        refs = []
    if not isinstance(refs, list):
        refs = []
    identity = (new.get("wiki_rel"), new.get("original_path"))
    refs = [item for item in refs if isinstance(item, dict) and (item.get("wiki_rel"), item.get("original_path")) != identity]
    refs.append(new)
    return _json(refs)


def _backup_database(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"banxue-pre-wiki-import-{stamp}.db"
    source = sqlite3.connect(path)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


def _import_page_evidence(conn, source_id: int, digest: str, old_digest: str,
                          page: WikiPage) -> list[int]:
    """Version Wiki evidence using the same stale/current contract as file scans."""
    if old_digest != digest:
        if old_digest:
            old_snapshot = sources._source_snapshot(conn, source_id, old_digest)
            conn.execute('''UPDATE source_chunks SET snapshot_id=?
                             WHERE source_id=? AND stale=0 AND snapshot_id IS NULL''',
                         (old_snapshot, source_id))
            conn.execute('''UPDATE evidence SET snapshot_id=?
                             WHERE source_id=? AND stale=0 AND snapshot_id IS NULL''',
                         (old_snapshot, source_id))
        sources._retire_stale_source_rows(conn, source_id)

    snapshot_id = sources._source_snapshot(conn, source_id, digest)
    if not old_digest or old_digest != digest:
        blocks = _blocks(page.path.read_text(encoding='utf-8', errors='replace'))
        first_position = conn.execute(
            'SELECT COALESCE(MAX(position),-1)+1 FROM source_chunks WHERE source_id=?',
            (source_id,)).fetchone()[0]
        chunk_ids = []
        for offset, (_, _, text) in enumerate(blocks):
            cursor = conn.execute(
                '''INSERT INTO source_chunks(source_id,position,content,snapshot_id,stale)
                   VALUES(?,?,?,?,0)''',
                (source_id, first_position + offset, text, snapshot_id))
            chunk_ids.append(int(cursor.lastrowid))
        evidence_ids = []
        for index, (start, end, text) in enumerate(blocks):
            cursor = conn.execute(
                '''INSERT INTO evidence
                   (source_id,chunk_id,locator_type,locator_start,locator_end,content,content_hash,snapshot_id,stale)
                   VALUES(?,?,?,?,?,?,?,?,0)
                   ON CONFLICT(source_id,locator_type,locator_start,locator_end,content_hash)
                   DO UPDATE SET chunk_id=excluded.chunk_id,snapshot_id=excluded.snapshot_id,
                                 stale=0,content=excluded.content''',
                (source_id, chunk_ids[index] if index < len(chunk_ids) else None,
                 'line', start, end, text,
                 hashlib.sha256(text.encode('utf-8')).hexdigest(), snapshot_id))
            evidence = conn.execute(
                '''SELECT id FROM evidence WHERE source_id=? AND locator_type='line'
                     AND locator_start=? AND locator_end=? AND content_hash=?''',
                (source_id, start, end, hashlib.sha256(text.encode('utf-8')).hexdigest())
            ).fetchone()
            evidence_ids.append(int(evidence['id']))
        return evidence_ids

    # Backfill snapshot links for pages imported before schema 022 without
    # generating duplicate evidence or chunks on an unchanged re-import.
    conn.execute('''UPDATE source_chunks SET snapshot_id=?
                     WHERE source_id=? AND stale=0 AND snapshot_id IS NULL''',
                 (snapshot_id, source_id))
    conn.execute('''UPDATE evidence SET snapshot_id=?
                     WHERE source_id=? AND stale=0 AND snapshot_id IS NULL''',
                 (snapshot_id, source_id))
    return [int(row['id']) for row in conn.execute(
        'SELECT id FROM evidence WHERE source_id=? AND stale=0 ORDER BY id',
        (source_id,)).fetchall()]


def _update_wiki_node(conn, kp_id: int, course_id: int, course_name: str,
                      imported: dict[str, str]) -> int:
    """Apply source-owned fields while retaining fields edited in the app."""
    protected = {row['field'] for row in conn.execute(
        "SELECT DISTINCT field FROM revisions WHERE kp_id=? AND origin<>'wiki_import'",
        (kp_id,)).fetchall()}
    if 'chapter' in protected:
        protected.add('chapter_key')
    current = conn.execute('SELECT * FROM kp WHERE id=?', (kp_id,)).fetchone()
    preserved = 0
    for field, value in imported.items():
        if field in protected:
            preserved += 1
            continue
        old_value = str(current[field] or '')
        value = str(value or '')
        if old_value == value:
            continue
        if field == 'name':
            duplicate = conn.execute(
                'SELECT 1 FROM kp WHERE course_id=? AND name=? AND id<>?',
                (course_id, value, kp_id)).fetchone()
            if duplicate:
                raise ValueError(f'Wiki 页面标题与现有知识点重名：{value}')
            conn.execute('INSERT OR IGNORE INTO kp_aliases(kp_id,alias,origin) VALUES(?,?,?)',
                         (kp_id, old_value, 'wiki_import'))
            from . import repo
            new_key = repo._node_key(course_id, value)
            if repo._node_key_conflict(conn, course_id, new_key, kp_id) is not None:
                raise ValueError(f'Wiki 页面标题的规范化身份与现有知识点冲突：{value}')
            repo._retarget_identity_links(conn, kp_id, course_name,
                                          old_value, value, new_key)
        conn.execute(f"UPDATE kp SET {field}=?,updated_at=datetime('now','localtime') WHERE id=?",
                     (value, kp_id))
        conn.execute('''INSERT INTO revisions(kp_id,field,old_value,new_value,origin,confirmed)
                         VALUES(?,?,?,?, 'wiki_import',1)''',
                     (kp_id, field, old_value, value))
    return preserved



def _page_name(page: WikiPage, used: set[str]) -> str:
    base = page.title.strip() or Path(page.relative).stem
    normalized_used = {' '.join(name.split()).casefold() for name in used}
    if ' '.join(base.split()).casefold() not in normalized_used:
        return base
    suffix = page.chapter.replace("/", " · ")
    candidate = f"{base} · {suffix}"
    serial = 2
    while ' '.join(candidate.split()).casefold() in normalized_used:
        candidate = f"{base} · {suffix} · {serial}"
        serial += 1
    return candidate


def _resolve_link(target: str, page: WikiPage, maps: dict[str, list[tuple[int, str]]]) -> int | None:
    key = target.strip().replace("\\", "/")
    if key.startswith(('./', '../')):
        key = posixpath.normpath(posixpath.join(posixpath.dirname(page.relative), key))
        options = list(dict.fromkeys(maps.get(key, [])))
        ids = list(dict.fromkeys(item[0] for item in options))
        return ids[0] if len(ids) == 1 else None
    options = list(dict.fromkeys(maps.get(key, [])))
    ids = list(dict.fromkeys(item[0] for item in options))
    if len(ids) == 1:
        return ids[0]
    local_ids = list(dict.fromkeys(item[0] for item in options if item[1] == page.chapter))
    if len(local_ids) == 1:
        return local_ids[0]
    if '/' in key:
        return None
    stem = Path(key).stem
    options = list(dict.fromkeys(maps.get(stem, [])))
    ids = list(dict.fromkeys(item[0] for item in options))
    if len(ids) == 1:
        return ids[0]
    local_ids = list(dict.fromkeys(item[0] for item in options if item[1] == page.chapter))
    if len(local_ids) == 1:
        return local_ids[0]
    return None


# ── 导入核心（幂等建图，正式写入通道）────────────────────


def _check_course(course: str, workspace: str = WORKPAL_WORKSPACE) -> str:
    """Validate a course name that becomes one directory segment below WorkPal."""
    name = str(course or '').strip()
    if not name:
        raise ValueError('课程名称不能为空')
    if len(name) > 80:
        raise ValueError('课程名称不能超过 80 个字符')
    if '/' in name or '\\' in name or name in {'.', '..'} or any(ord(c) < 32 for c in name):
        raise ValueError('课程名称不能包含路径分隔符')
    if not str(workspace or '').strip():
        raise ValueError('工作区不能为空')
    return name


def _course_id_if_present(conn, course: str, workspace: str) -> int | None:
    row = conn.execute(
        """SELECT c.id FROM courses c JOIN workspaces w ON w.id=c.workspace_id
            WHERE c.name=? AND w.key=?""",
        (course, workspace)).fetchone()
    return int(row['id']) if row else None


def _ensure_course(conn, course: str, workspace: str) -> int:
    """Return the course id, auto-creating the WorkPal course + inbox directory."""
    course_id = _course_id_if_present(conn, course, workspace)
    if course_id is not None:
        return course_id
    from . import repo
    try:
        repo.create_course(course, workspace=workspace)
    except ValueError:
        # A concurrent caller may have created it between the read and the insert.
        course_id = _course_id_if_present(conn, course, workspace)
        if course_id is None:
            raise
        return course_id
    inbox = db.DATA_DIR / 'WorkPal' / course / 'inbox'
    try:
        inbox.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning('WorkPal 收件目录创建失败，导入继续：%s', inbox)
    course_id = _course_id_if_present(conn, course, workspace)
    if course_id is None:
        raise ValueError(f'WorkPal 课程创建失败：{course}')
    return course_id


def _wiki_inventory(course: str, workspace: str) -> tuple[list[dict], dict[str, dict]]:
    """Read imported Wiki identities without creating or upgrading a database."""
    if not db.DB_PATH.is_file():
        return [], {}
    conn = sqlite3.connect(db.DB_PATH.resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            '''SELECT kp.id, kp.record_id, kp.name, kp.mastery, kp.source,
                      m.attributes_json, m.source_refs_json, sf.id AS source_id,
                      sf.path, sf.sha256
                 FROM kp
                 JOIN courses c ON c.id=kp.course_id
                 JOIN workspaces w ON w.id=c.workspace_id
            LEFT JOIN knowledge_node_meta m ON m.kp_id=kp.id
            LEFT JOIN source_files sf ON sf.course_id=kp.course_id AND sf.path=kp.source
                WHERE c.name=? AND w.key=? AND kp.record_id LIKE 'wiki:%' ''',
            (course, workspace)).fetchall()
        source_rows = conn.execute(
            '''SELECT sf.id,sf.path,sf.sha256 FROM source_files sf
                 JOIN courses c ON c.id=sf.course_id
                 JOIN workspaces w ON w.id=c.workspace_id
                WHERE c.name=? AND w.key=?''', (course, workspace)).fetchall()
        sources_by_path = {row['path']: dict(row) for row in source_rows}
        result = []
        for row in rows:
            try:
                attrs = json.loads(row['attributes_json'] or '{}')
            except json.JSONDecodeError:
                attrs = {}
            try:
                refs = json.loads(row['source_refs_json'] or '[]')
            except json.JSONDecodeError:
                refs = []
            relative = str(row['record_id'])[5:]
            if not isinstance(attrs, dict) or not isinstance(refs, list):
                raise ValueError('Wiki 身份元数据格式无效，请先修复导入记录')
            canonical_path = str(db.DATA_DIR / 'WorkPal' / course / 'wiki' / relative)
            source = sources_by_path.get(canonical_path, {})
            result.append({
                'id': int(row['id']), 'record_id': str(row['record_id']),
                'name': str(row['name'] or ''), 'mastery': str(row['mastery'] or ''),
                'source': str(row['source'] or ''), 'source_id': source.get('id'),
                'path': str(source.get('path') or ''), 'sha256': str(source.get('sha256') or ''),
                'relative': str(attrs.get('wiki_rel') or relative),
                'uid': str(attrs.get('wiki_uid') or '').strip(),
                'path_history': list(attrs.get('wiki_path_history') or []),
                'refs': refs if isinstance(refs, list) else [],
            })
        return result, sources_by_path
    finally:
        conn.close()


def _change_plan(root: Path, pages: list[WikiPage], course: str,
                 workspace: str) -> dict:
    """Build a non-mutating import plan for preview and preflight validation."""
    existing, sources_by_path = _wiki_inventory(course, workspace)
    by_path = {row['relative']: row for row in existing}
    by_uid = {row['uid']: row for row in existing if row['uid']}
    changes: list[dict] = []
    conflicts: list[dict] = []
    incoming_uids: dict[str, str] = {}
    seen_existing: set[int] = set()
    copy_root = db.DATA_DIR / 'WorkPal' / course / 'wiki'
    if len(by_path) != len(existing) or len(by_uid) != sum(bool(row['uid']) for row in existing):
        conflicts.append({'kind': 'stored_identity_collision'})
    for row in existing:
        for ref in row['refs']:
            if not isinstance(ref, dict) or not ref.get('wiki_rel') or not ref.get('original_path'):
                continue
            old_root = Path(ref['original_path'])
            for _ in Path(ref['wiki_rel']).parts:
                old_root = old_root.parent
            if old_root.resolve() != root:
                raise ValueError('此主题已接入另一个 Wiki；请为新 Wiki 指定不同主题')
    for page in pages:
        if page.uid:
            prior = incoming_uids.get(page.uid)
            if prior and prior != page.relative:
                conflicts.append({'kind': 'duplicate_identity', 'uid': page.uid,
                                  'paths': [prior, page.relative]})
            incoming_uids[page.uid] = page.relative
        current = by_path.get(page.relative)
        identity = by_uid.get(page.uid) if page.uid else None
        if current and current.get('uid') and current['uid'] != page.uid:
            conflicts.append({'kind': 'identity_replaced', 'relative': page.relative,
                              'old_uid': current['uid'], 'new_uid': page.uid})
        if identity and current and identity['id'] != current['id']:
            conflicts.append({'kind': 'identity_path_collision', 'uid': page.uid,
                              'paths': [identity['relative'], current['relative']]})
        matched = identity or current
        destination = copy_root / page.relative
        _check_destination(destination)
        destination_source = sources_by_path.get(str(destination.resolve()))
        if destination_source and (not matched or destination_source['id'] != matched['source_id']):
            conflicts.append({'kind': 'occupied_source_identity', 'relative': page.relative})
        if not matched and destination.exists():
            conflicts.append({'kind': 'untracked_destination', 'relative': page.relative})
        if matched:
            if destination.is_file() and destination_source:
                local_digest = hashlib.sha256(destination.read_text(
                    encoding='utf-8-sig', errors='replace').encode('utf-8')).hexdigest()
                if local_digest not in {destination_source['sha256'], page.digest}:
                    conflicts.append({'kind': 'local_copy_changed', 'relative': page.relative})
            if matched['id'] in seen_existing:
                conflicts.append({'kind': 'multiple_pages_one_identity',
                                  'relative': page.relative})
            seen_existing.add(matched['id'])
            if identity and identity['relative'] != page.relative:
                kind = 'moved'
                if not matched['source_id']:
                    conflicts.append({'kind': 'missing_source_identity', 'relative': page.relative})
                if destination.exists():
                    conflicts.append({'kind': 'occupied_destination', 'relative': page.relative})
            elif matched.get('sha256') == page.digest:
                kind = 'unchanged'
            else:
                kind = 'updated'
        else:
            kind = 'added'
        changes.append({
            'kind': kind, 'relative': page.relative, 'uid': page.uid,
            'previous_relative': matched['relative'] if matched and kind == 'moved' else '',
            'name': page.title, 'requires_review': False,
            'kp_id': matched['id'] if matched else None,
            'source_id': matched['source_id'] if matched else None,
        })
    for row in existing:
        if row['id'] not in seen_existing:
            changes.append({'kind': 'missing', 'relative': row['relative'],
                            'uid': row['uid'], 'name': row['name'],
                            'requires_review': True})
    counts = {key: sum(item['kind'] == key for item in changes)
              for key in ('added', 'unchanged', 'updated', 'moved', 'missing')}
    return {'changes': changes, 'conflicts': conflicts,
            'change_counts': counts, 'can_apply': not conflicts}


@sources._serialized_scan
def import_pages(wiki_root: Path, course: str = DEFAULT_COURSE, *, apply: bool = True,
                 progress=None, workspace: str = WORKPAL_WORKSPACE,
                 allow_api: bool = False) -> dict:
    """Import one compiled wiki directory into the WorkPal graph (idempotent).

    apply=False previews changes without writing. progress fires once per page.
    A missing course is created inside the WorkPal workspace (with its inbox
    directory); every formal graph write stays behind the audited channel.
    """
    course = _check_course(course, workspace)
    if workspace != WORKPAL_WORKSPACE:
        raise ValueError('Wiki 导入目前只支持 WorkPal')
    root = resolve_wiki_root(wiki_root)
    pages = iter_pages(root)
    total = len(pages)
    if apply and not total:
        raise ValueError('目录中没有可导入的 Markdown 页面')
    copy_root = db.DATA_DIR / 'WorkPal' / course / 'wiki'
    _check_destination(copy_root)
    if root.is_relative_to(copy_root.resolve()) or copy_root.resolve().is_relative_to(root):
        raise ValueError('源 Wiki 与导入目录不能重叠')
    summary = {
        'wiki_root': str(root),
        'workspace': workspace,
        'course': course,
        'pages': total,
        'excluded_markdown': sum(1 for _ in _markdown_paths(root)) - total,
        'copied': 0,
        'sources_created': 0,
        'sources_updated': 0,
        'nodes_created': 0,
        'nodes_updated': 0,
        'manual_fields_preserved': 0,
        'edges_created': 0,
        'unresolved_links': 0,
        'unresolved_details': [],
        'parse_warnings': [page.relative for page in pages if page.parse_warning],
        'backup': '',
        'dry_run': not apply,
    }

    def _tick(processed: int) -> None:
        if progress is not None:
            progress(processed, total)

    summary.update(_change_plan(root, pages, course, workspace))
    if not apply:
        names: set[str] = set()
        for page_index, page in enumerate(pages, 1):
            _page_name(page, names)
            names.add(page.title)
            _tick(page_index)
        summary['unique_titles'] = len(names)
        summary['links'] = sum(len(page.links) for page in pages)
        return summary

    if not summary['can_apply']:
        raise ValueError('Wiki 身份冲突，请先查看导入预览并修正 id/uid 或路径')

    # T2.9/O-DATA-7: a maintenance import writes the formal graph only through
    # this audited channel (allow-listed script + ingest_runs audit row).
    channel = sources.authorize_formal_write('import_wiki_workpal', allow_api=allow_api)
    conn = db.connect()
    try:
        previous_id = _course_id_if_present(conn, course, workspace)
        if previous_id:
            for row in conn.execute('''SELECT m.source_refs_json FROM knowledge_node_meta m
                                       JOIN kp ON kp.id=m.kp_id WHERE kp.course_id=?''', (previous_id,)):
                for ref in json.loads(row['source_refs_json'] or '[]'):
                    if not isinstance(ref, dict) or not ref.get('wiki_rel') or not ref.get('original_path'):
                        continue
                    old_root = Path(ref['original_path'])
                    for _ in Path(ref['wiki_rel']).parts:
                        old_root = old_root.parent
                    if old_root.resolve() != root:
                        raise ValueError('此主题已接入另一个 Wiki；请为新 Wiki 指定不同主题')
        summary['backup'] = str(_backup_database(db.DB_PATH))
        course_id = _ensure_course(conn, course, workspace)
        names = {row['name'] for row in conn.execute('SELECT name FROM kp WHERE course_id=?', (course_id,))}
        page_records: list[tuple[WikiPage, str, Path, int, list[int]]] = []
        by_key: dict[str, list[tuple[int, str]]] = {}
        planned = {item['relative']: item for item in summary['changes']
                   if item['kind'] != 'missing'}
        with conn:
            for page_index, page in enumerate(pages, 1):
                change = planned[page.relative]
                existing = conn.execute('SELECT * FROM kp WHERE course_id=? AND id=?',
                                        (course_id, change['kp_id'])).fetchone()
                if existing:
                    names.discard(existing['name'])
                name = _page_name(page, names)
                if existing and conn.execute(
                        "SELECT 1 FROM revisions WHERE kp_id=? AND field='name' AND origin<>'wiki_import' LIMIT 1",
                        (existing['id'],)).fetchone():
                    name = existing['name']
                names.add(name)
                local_path = (copy_root / page.relative).resolve()
                if change['kind'] == 'moved':
                    occupied = conn.execute('SELECT id FROM source_files WHERE course_id=? AND path=?',
                                            (course_id, str(local_path))).fetchone()
                    if occupied and occupied['id'] != change['source_id']:
                        raise ValueError('Wiki 移动目标已有其他来源，不能覆盖')
                    if local_path.exists():
                        raise ValueError('Wiki 移动目标已有文件，请先处理冲突')
                local_path = _copy_path(page, copy_root)
                summary["copied"] += 1
                if change['kind'] == 'moved':
                    conn.execute('UPDATE source_files SET path=? WHERE id=? AND course_id=?',
                                 (str(local_path), change['source_id'], course_id))
                    conn.execute('UPDATE kp SET record_id=? WHERE id=?',
                                 (f'wiki:{page.relative}', existing['id']))
                    conn.execute('''INSERT INTO revisions(kp_id,field,old_value,new_value,origin,confirmed)
                                     VALUES(?, 'record_id', ?, ?, 'wiki_import',1)''',
                                 (existing['id'], existing['record_id'], f'wiki:{page.relative}'))
                old_source = conn.execute("SELECT id,sha256 FROM source_files WHERE course_id=? AND path=?", (course_id, str(local_path))).fetchone()
                conn.execute(
                    """INSERT INTO source_files(course_id,path,sha256,status,error,indexed_at,source_kind,source_role,catalog_status)
                       VALUES(?,?,?,'indexed','',datetime('now','localtime'),'wiki_page','wiki_page','none')
                       ON CONFLICT(course_id,path) DO UPDATE SET sha256=excluded.sha256,status='indexed',error='',indexed_at=excluded.indexed_at,
                       source_kind=excluded.source_kind,source_role=excluded.source_role,catalog_status=excluded.catalog_status""",
                    (course_id, str(local_path), page.digest),
                )
                source_id = int(conn.execute("SELECT id FROM source_files WHERE course_id=? AND path=?", (course_id, str(local_path))).fetchone()[0])
                summary["sources_updated" if old_source else "sources_created"] += 1
                evidence_ids = _import_page_evidence(
                    conn, source_id, page.digest,
                    str(old_source['sha256'] or '') if old_source else '', page)
                conn.execute("INSERT OR IGNORE INTO source_versions(source_id,sha256,status) VALUES(?,?, 'indexed')", (source_id, page.digest))
                node_type = _node_type(page.page_type)
                detail = _plain_excerpt(page.body, MAX_DETAIL)
                related = ", ".join(page.links[:30])
                if existing:
                    kp_id = int(existing["id"])
                    summary['manual_fields_preserved'] += _update_wiki_node(
                        conn, kp_id, course_id, course, {
                            'name': name,
                            'band': page.tags[0] if page.tags else 'Wiki',
                            'chapter': page.chapter,
                            'chapter_key': page.chapter_key,
                            'gist': page.gist,
                            'detail': detail,
                            'source': str(local_path),
                            'related': related,
                            'kp_type': node_type,
                        })
                    summary["nodes_updated"] += 1
                else:
                    cur = conn.execute(
                        """INSERT INTO kp(course_id,record_id,name,band,chapter,chapter_key,section_key,gist,detail,source,related,kp_type,freq,mastery)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'未读')""",
                        (course_id, f"wiki:{page.relative}", name, page.tags[0] if page.tags else 'Wiki', page.chapter, page.chapter_key, '', page.gist, detail, str(local_path), related, node_type, 'wiki'),
                    )
                    kp_id = int(cur.lastrowid)
                    summary["nodes_created"] += 1
                    for field, value in {
                        'name': name, 'band': page.tags[0] if page.tags else 'Wiki',
                        'chapter': page.chapter, 'chapter_key': page.chapter_key,
                        'gist': page.gist, 'detail': detail, 'source': str(local_path),
                        'related': related, 'kp_type': node_type,
                    }.items():
                        if value:
                            conn.execute('''INSERT INTO revisions
                                (kp_id,field,old_value,new_value,origin,confirmed)
                                VALUES(?,?, '', ?, 'wiki_import',1)''',
                                (kp_id, field, str(value)))
                workspace_id = conn.execute(
                    'SELECT workspace_id FROM courses WHERE id=?', (course_id,)).fetchone()['workspace_id']
                if workspace_id is None:
                    raise ValueError(f'课程未绑定工作区：{course}')
                documents.ensure_imported_wiki_document(
                    conn, workspace_id=int(workspace_id), course_id=course_id,
                    kp_id=kp_id, title=name, body_md=page.body)
                if evidence_ids:
                    conn.executemany("INSERT OR IGNORE INTO kp_evidence(kp_id,evidence_id,role) VALUES(?,?, 'supports')", [(kp_id, eid) for eid in evidence_ids[:5]])
                ref = {
                    "wiki_rel": page.relative,
                    "stored_path": str(local_path),
                    "original_path": str(page.path),
                    "source_ref": page.source_ref,
                    "quality": "wiki",
                    "wiki_uid": page.uid,
                }
                old_meta = conn.execute("SELECT attributes_json,source_refs_json,status FROM knowledge_node_meta WHERE kp_id=?", (kp_id,)).fetchone()
                attributes = {}
                if old_meta:
                    try:
                        attributes = json.loads(old_meta["attributes_json"] or '{}')
                    except json.JSONDecodeError:
                        attributes = {}
                attributes.update({"wiki_type": page.page_type, "tags": list(page.tags), "wiki_rel": page.relative})
                if page.uid:
                    attributes['wiki_uid'] = page.uid
                if change['kind'] == 'moved':
                    attributes['wiki_path_history'] = list(dict.fromkeys([
                        *attributes.get('wiki_path_history', []), change['previous_relative']]))
                meta_status = old_meta["status"] if old_meta else _status(page.knowledge_status)
                old_refs = old_meta["source_refs_json"] if old_meta else '[]'
                conn.execute(
                    """INSERT INTO knowledge_node_meta(kp_id,schema_version,domain,attributes_json,source_refs_json,status)
                       VALUES(?,?,?,?,?,?)
                       ON CONFLICT(kp_id) DO UPDATE SET domain='work',attributes_json=excluded.attributes_json,source_refs_json=excluded.source_refs_json,
                       updated_at=datetime('now','localtime')""",
                    (kp_id, SCHEMA_VERSION, 'work', _json(attributes), _merge_refs(old_refs, ref), meta_status),
                )
                page_records.append((page, name, local_path, kp_id, evidence_ids))
                for key in (page.relative[:-3], Path(page.relative).stem, page.title, name, *page.aliases):
                    by_key.setdefault(key, []).append((kp_id, page.chapter))
                _tick(page_index)
            for page, _, _, from_id, evidence_ids in page_records:
                # Reconcile only relationships owned by this importer, not manual edges.
                targets = {target: _resolve_link(target, page, by_key) for target in page.links}
                retained_ids = {target_id for target_id in targets.values() if target_id}
                for edge in conn.execute('''SELECT e.id,e.to_kp FROM kp_edges e
                                            JOIN knowledge_relation_meta m ON m.edge_id=e.id
                                            WHERE e.from_kp=? AND m.status='confirmed' AND m.reason LIKE 'Wiki 显式链接：%' ''', (from_id,)).fetchall():
                    if edge['to_kp'] not in retained_ids:
                        conn.execute('DELETE FROM kp_edges WHERE id=?', (edge['id'],))
                for target in page.links:
                    to_id = targets[target]
                    if not to_id or to_id == from_id:
                        summary["unresolved_links"] += 1
                        if len(summary['unresolved_details']) < 200:
                            summary['unresolved_details'].append({'page': page.relative, 'target': target})
                        continue
                    before = conn.execute("SELECT id FROM kp_edges WHERE from_kp=? AND to_kp=? AND kind='related'", (from_id, to_id)).fetchone()
                    conn.execute("INSERT OR IGNORE INTO kp_edges(from_kp,to_kp,kind) VALUES(?,?, 'related')", (from_id, to_id))
                    edge = conn.execute("SELECT id FROM kp_edges WHERE from_kp=? AND to_kp=? AND kind='related'", (from_id, to_id)).fetchone()
                    evidence_id = evidence_ids[0] if evidence_ids else None
                    conn.execute(
                        """INSERT INTO knowledge_relation_meta(edge_id,evidence_id,reason,confidence,status)
                           VALUES(?,?,?,?, 'confirmed')
                           ON CONFLICT(edge_id) DO UPDATE SET evidence_id=excluded.evidence_id,reason=excluded.reason,confidence=excluded.confidence,
                           updated_at=datetime('now','localtime')
                           WHERE knowledge_relation_meta.reason LIKE 'Wiki 显式链接：%'
                             AND knowledge_relation_meta.status='confirmed'""",
                        (int(edge[0]), evidence_id, f"Wiki 显式链接：[[{target}]]", 1.0),
                    )
                    if not before:
                        summary["edges_created"] += 1
        summary["audit_run_id"] = sources.record_formal_write(
            channel, course_id=course_id,
            nodes=summary["nodes_created"] + summary["nodes_updated"],
            edges=summary["edges_created"],
            detail=json.dumps({key: summary[key] for key in
                               ("pages", "nodes_created", "nodes_updated", "edges_created",
                                "manual_fields_preserved")},
                              ensure_ascii=False))
        return summary
    finally:
        conn.close()


# ── Obsidian 仓库发现 ─────────────────────────────────


def _default_config_paths() -> list[Path]:
    system = platform.system().lower()
    home = Path.home()
    if system == 'darwin':
        return [home / 'Library' / 'Application Support' / 'obsidian' / 'obsidian.json']
    if system == 'windows':
        appdata = os.environ.get('APPDATA', '')
        return [Path(appdata) / 'obsidian' / 'obsidian.json'] if appdata else [
            home / 'AppData' / 'Roaming' / 'obsidian' / 'obsidian.json']
    return [home / '.config' / 'obsidian' / 'obsidian.json']


def _config_vault_paths(config_path: Path) -> list[str]:
    try:
        payload = json.loads(config_path.expanduser().read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    vaults = payload.get('vaults') if isinstance(payload, dict) else None
    if not isinstance(vaults, dict):
        return []
    result = []
    for item in vaults.values():
        if not isinstance(item, dict):
            continue
        raw = str(item.get('path') or '').strip()
        if raw:
            result.append(raw)
    return result


def vault_candidates(config_paths: list[Path] | None = None) -> list[dict]:
    """Discover local Obsidian vaults; a wiki/ child directory is preferred.

    Missing, unreadable or malformed configs are skipped instead of raising, so
    the import dialog can always render with an empty candidate list.
    """
    paths = [Path(item) for item in (config_paths or _default_config_paths())]
    candidates: dict[str, dict] = {}
    for config_path in paths:
        for raw in _config_vault_paths(config_path):
            try:
                vault = Path(raw).expanduser().resolve()
            except (OSError, RuntimeError, ValueError):
                continue
            if not vault.is_dir():
                continue
            wiki = vault / 'wiki'
            has_wiki = wiki.is_dir()
            target = wiki if has_wiki else vault
            key = str(target)
            if key in candidates:
                continue
            candidates[key] = {'path': key, 'label': vault.name or key,
                               'has_wiki': has_wiki}
    return sorted(candidates.values(), key=lambda item: item['path'])


# ── 后台导入任务（单并发状态机）──────────────────────────

_JOB_LOCK = threading.Lock()
_JOB_STATE: dict = {
    'state': 'idle',
    'started_at': '',
    'finished_at': '',
    'wiki_root': '',
    'course': '',
    'total': 0,
    'done': 0,
    'result': None,
    'error': '',
}


def _job_snapshot() -> dict:
    with _JOB_LOCK:
        snapshot = dict(_JOB_STATE)
        if isinstance(snapshot.get('result'), dict):
            snapshot['result'] = dict(snapshot['result'])
        return snapshot


def status() -> dict:
    """Return a copy of the current import job state; never raises."""
    return _job_snapshot()


def reset_job_state() -> None:
    """Clear the job state (startup recovery / tests)."""
    with _JOB_LOCK:
        _JOB_STATE.update({'state': 'idle', 'started_at': '', 'finished_at': '',
                           'wiki_root': '', 'course': '', 'total': 0, 'done': 0,
                           'result': None, 'error': ''})


def start_import(vault_root: Path | str, course: str = DEFAULT_COURSE) -> dict:
    """Start one background import; while running the current state is returned.

    The API maps a running state to 409 by checking status() before the call.
    """
    root = resolve_wiki_root(vault_root)
    course = _check_course(course, WORKPAL_WORKSPACE)
    with _JOB_LOCK:
        if _JOB_STATE['state'] == 'running':
            return dict(_JOB_STATE)
        _JOB_STATE.update({
            'state': 'running',
            'started_at': datetime.now().isoformat(timespec='seconds'),
            'finished_at': '', 'wiki_root': str(root), 'course': course,
            'total': 0, 'done': 0, 'result': None, 'error': '',
        })

    def _progress(processed: int, total: int) -> None:
        with _JOB_LOCK:
            _JOB_STATE['done'] = int(processed)
            _JOB_STATE['total'] = int(total)

    def _worker() -> None:
        try:
            result = import_pages(root, course, apply=True, progress=_progress,
                                  allow_api=True)
        except Exception as exc:  # noqa: BLE001 - background job records failures
            logger.exception('WorkPal Wiki 导入失败：%s', root)
            with _JOB_LOCK:
                _JOB_STATE.update({
                    'state': 'error', 'error': str(exc)[:500],
                    'finished_at': datetime.now().isoformat(timespec='seconds'),
                })
            return
        with _JOB_LOCK:
            _JOB_STATE.update({
                'state': 'done', 'result': result,
                'total': int(result.get('pages') or _JOB_STATE['total']),
                'done': int(result.get('pages') or _JOB_STATE['done']),
                'finished_at': datetime.now().isoformat(timespec='seconds'),
            })

    logger.info('WorkPal Wiki 导入开始：%s', root)
    threading.Thread(target=_worker, name='workpal-wiki-import', daemon=True).start()
    return _job_snapshot()
