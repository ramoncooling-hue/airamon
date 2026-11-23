from __future__ import annotations

import json
import os
import re
import tempfile
import io
import zipfile
from pathlib import Path

from ..memory.db import ROOT, connect
from ..runtime import data_dir

# Packaged source files are templates. Runtime copies are the editable Hub.
# Keeping them under the data root makes the source bundle safe to install in
# a read-only location and keeps Agent/Skill edits with the user's own data.
TEMPLATE_SKILLS_DIR = ROOT / 'skills'
TEMPLATE_AGENTS_DIR = ROOT / 'agents'
HUB_DIR = data_dir() / 'app' / 'hub'
DEFAULT_SKILLS_DIR = HUB_DIR / 'skills'
DEFAULT_AGENTS_DIR = HUB_DIR / 'agents'
SKILLS_DIR = DEFAULT_SKILLS_DIR
AGENTS_DIR = DEFAULT_AGENTS_DIR
KEY_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')
MAX_CONTENT_BYTES = 512 * 1024
EDITABLE_MANIFEST_FIELDS = {'name', 'description', 'version', 'skills'}
SECRET_FIELD_RE = re.compile(r'(api[_-]?key|token|password|secret|cookie)', re.IGNORECASE)


def _manifest(directory: Path, required: str) -> dict:
    path = directory / 'manifest.json'
    if not path.is_file():
        raise ValueError(f'缺少 {required} manifest.json')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise ValueError(f'{required} manifest.json 不是有效 JSON') from exc
    for key in ('key', 'name', 'version'):
        if not str(data.get(key, '')).strip():
            raise ValueError(f'{required} manifest 缺少 {key}')
    return data


def _kind_config(kind: str) -> tuple[Path, str, str]:
    if kind == 'skill':
        return SKILLS_DIR, 'skills', 'SKILL.md'
    if kind == 'agent':
        return AGENTS_DIR, 'agents', 'system.md'
    raise ValueError('类型只能是 skill 或 agent')


def _safe_key(key: str) -> str:
    value = str(key or '').strip()
    if not KEY_RE.fullmatch(value):
        raise ValueError('能力标识只能包含字母、数字、下划线和短横线')
    return value


def _atomic_write(path: Path, content: str) -> None:
    encoded = content.encode('utf-8')
    if len(encoded) > MAX_CONTENT_BYTES:
        raise ValueError('技能内容不能超过 512 KB')
    fd, temp_name = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _bootstrap_templates() -> None:
    """Copy packaged Hub templates once without overwriting user edits."""
    # Tests and embedding callers can replace the directories explicitly.
    if SKILLS_DIR != DEFAULT_SKILLS_DIR or AGENTS_DIR != DEFAULT_AGENTS_DIR:
        return
    for template_root, runtime_root in (
        (TEMPLATE_SKILLS_DIR, SKILLS_DIR),
        (TEMPLATE_AGENTS_DIR, AGENTS_DIR),
    ):
        runtime_root.mkdir(parents=True, exist_ok=True)
        if not template_root.is_dir():
            continue
        for template in sorted(template_root.iterdir()):
            if not template.is_dir():
                continue
            target = runtime_root / template.name
            target.mkdir(parents=True, exist_ok=True)
            for filename in ('manifest.json', 'SKILL.md', 'system.md'):
                source = template / filename
                destination = target / filename
                if source.is_file() and not destination.exists():
                    destination.write_bytes(source.read_bytes())


def _validate_manifest_patch(body: dict, manifest: dict, key: str, kind: str) -> dict:
    if not isinstance(body, dict):
        raise ValueError('更新内容必须是对象')
    unknown = set(body) - (EDITABLE_MANIFEST_FIELDS | {'content', 'enabled'})
    if unknown:
        raise ValueError(f'不支持修改字段：{sorted(unknown)[0]}')
    result = dict(manifest)
    for field in EDITABLE_MANIFEST_FIELDS:
        if field not in body:
            continue
        value = body[field]
        if field == 'skills':
            if kind != 'agent' or not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ValueError('只有 agent 可以设置 skills，且必须是字符串数组')
            value = [_safe_key(item) for item in value]
        elif not isinstance(value, str) or (field != 'description' and not value.strip()):
            raise ValueError(f'{field} 不能为空')
        result[field] = value
    result['key'] = key
    if any(SECRET_FIELD_RE.search(str(field)) for field in result):
        raise ValueError('manifest 不能保存密钥或凭据字段')
    return result


def sync() -> dict:
    _bootstrap_templates()
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        skills = []
        for directory in sorted(SKILLS_DIR.iterdir()):
            if not directory.is_dir():
                continue
            data = _manifest(directory, directory.name)
            skill_file = directory / 'SKILL.md'
            if not skill_file.is_file():
                raise ValueError(f"技能 {data['key']} 缺少 SKILL.md")
            conn.execute('''INSERT INTO skills(key,name,path,version,manifest_json)
                            VALUES(?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                            name=excluded.name,path=excluded.path,version=excluded.version,
                            manifest_json=excluded.manifest_json,updated_at=datetime('now','localtime')''',
                         (data['key'], data['name'], str(directory), data['version'], json.dumps(data, ensure_ascii=False)))
            skills.append(data)
        agents = []
        for directory in sorted(AGENTS_DIR.iterdir()):
            if not directory.is_dir():
                continue
            data = _manifest(directory, directory.name)
            if not (directory / 'system.md').is_file():
                raise ValueError(f"智能体 {data['key']} 缺少 system.md")
            conn.execute('''INSERT INTO agents(key,name,path,version,manifest_json)
                            VALUES(?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                            name=excluded.name,path=excluded.path,version=excluded.version,
                            manifest_json=excluded.manifest_json,updated_at=datetime('now','localtime')''',
                         (data['key'], data['name'], str(directory), data['version'], json.dumps(data, ensure_ascii=False)))
            agent_row = conn.execute('SELECT id FROM agents WHERE key=?', (data['key'],)).fetchone()
            conn.execute('DELETE FROM agent_skills WHERE agent_id=?', (agent_row['id'],))
            for priority, skill_key in enumerate(data.get('skills', [])):
                skill_row = conn.execute('SELECT id FROM skills WHERE key=?', (skill_key,)).fetchone()
                if skill_row:
                    conn.execute('INSERT OR REPLACE INTO agent_skills(agent_id,skill_id,priority) VALUES(?,?,?)',
                                 (agent_row['id'], skill_row['id'], priority))
            agents.append(data)
        conn.commit()
        return {'skills': skills, 'agents': agents}
    finally:
        conn.close()


def list_registry() -> dict:
    sync_result = sync()
    conn = connect()
    try:
        def public_rows(table: str, kind: str) -> list[dict]:
            rows = []
            for row in conn.execute(f'SELECT key,name,version,enabled,manifest_json FROM {table} ORDER BY name'):
                manifest = json.loads(row['manifest_json'] or '{}')
                bound_agent_count = 0
                revision_count = 0
                if kind == 'skill':
                    bound_agent_count = conn.execute(
                        '''SELECT COUNT(*) FROM agent_skills links
                           JOIN skills skill ON skill.id=links.skill_id
                           WHERE skill.key=?''', (row['key'],)
                    ).fetchone()[0]
                    revision_count = conn.execute(
                        'SELECT COUNT(*) FROM hub_revisions WHERE kind=? AND key=?',
                        ('skill', row['key'])
                    ).fetchone()[0]
                rows.append({
                    'kind': kind,
                    'key': row['key'],
                    'name': row['name'],
                    'version': row['version'],
                    'enabled': bool(row['enabled']),
                    'description': manifest.get('description', ''),
                    'skills': manifest.get('skills', []) if kind == 'agent' else [],
                    'bound_agent_count': bound_agent_count,
                    'revision_count': revision_count,
                })
            return rows
        skills = public_rows('skills', 'skill')
        agents = public_rows('agents', 'agent')
        return {'skills': skills, 'agents': agents, 'files': sync_result}
    finally:
        conn.close()


def set_enabled(kind: str, key: str, enabled: bool) -> dict:
    _kind_config(kind)
    key = _safe_key(key)
    table = 'skills' if kind == 'skill' else 'agents'
    conn = connect()
    try:
        cur = conn.execute(f'UPDATE {table} SET enabled=?, updated_at=datetime(\'now\',\'localtime\') WHERE key=?',
                           (1 if enabled else 0, key))
        if not cur.rowcount:
            raise ValueError('能力不存在')
        conn.commit()
    finally:
        conn.close()
    return get_detail(kind, key)


def get_detail(kind: str, key: str) -> dict:
    base, label, content_name = _kind_config(kind)
    key = _safe_key(key)
    directory = base / key
    manifest_path = directory / 'manifest.json'
    content_path = directory / content_name
    if not directory.is_dir() or not manifest_path.is_file() or not content_path.is_file():
        raise ValueError(f'{label} {key} 不存在或文件不完整')
    manifest = _manifest(directory, key)
    if manifest.get('key') != key:
        raise ValueError(f'{label} {key} 的 manifest key 不匹配')
    conn = connect()
    try:
        table = 'skills' if kind == 'skill' else 'agents'
        row = conn.execute(f'SELECT enabled FROM {table} WHERE key=?', (key,)).fetchone()
        bound_agent_count = 0
        if kind == 'skill':
            bound_agent_count = conn.execute(
                '''SELECT COUNT(*) FROM agent_skills links
                   JOIN skills skill ON skill.id=links.skill_id
                   WHERE skill.key=?''', (key,)
            ).fetchone()[0]
        revision_count = conn.execute(
            'SELECT COUNT(*) FROM hub_revisions WHERE kind=? AND key=?', (kind, key)
        ).fetchone()[0]
        return {
            'kind': kind,
            'key': key,
            'name': manifest['name'],
            'version': manifest['version'],
            'description': manifest.get('description', ''),
            'skills': manifest.get('skills', []) if kind == 'agent' else [],
            'enabled': bool(row['enabled']) if row else True,
            'content': content_path.read_text(encoding='utf-8'),
            'bound_agent_count': bound_agent_count,
            'revision_count': revision_count,
        }
    finally:
        conn.close()


def update(kind: str, key: str, body: dict) -> dict:
    base, label, content_name = _kind_config(kind)
    key = _safe_key(key)
    directory = base / key
    manifest_path = directory / 'manifest.json'
    content_path = directory / content_name
    if not directory.is_dir() or not manifest_path.is_file() or not content_path.is_file():
        raise ValueError(f'{label} {key} 不存在或文件不完整')
    manifest = _manifest(directory, key)
    next_manifest = _validate_manifest_patch(body, manifest, key, kind)
    content = body.get('content', content_path.read_text(encoding='utf-8'))
    if not isinstance(content, str) or not content.strip():
        raise ValueError('内容不能为空')
    if kind == 'agent' and 'skills' in body:
        _validate_agent_skill_bindings(
            next_manifest.get('skills', []),
            allowed_disabled=set(manifest.get('skills', [])),
        )
    old_content = content_path.read_text(encoding='utf-8')
    conn = connect()
    try:
        conn.execute('''INSERT INTO hub_revisions(kind,key,version,manifest_json,content)
                        VALUES(?,?,?,?,?)''',
                     (kind, key, manifest['version'], json.dumps(manifest, ensure_ascii=False), old_content))
        conn.commit()
    finally:
        conn.close()
    # Write the prompt and manifest as two atomic replacements. A failed
    # validation above leaves the registered artifact untouched.
    _atomic_write(content_path, content)
    _atomic_write(manifest_path, json.dumps(next_manifest, ensure_ascii=False, indent=2) + '\n')
    if 'enabled' in body:
        if not isinstance(body['enabled'], bool):
            raise ValueError('enabled 必须是布尔值')
        set_enabled(kind, key, body['enabled'])
    sync()
    return get_detail(kind, key)


def create_agent(body: dict) -> dict:
    """Create a new file-backed Agent without allowing arbitrary paths."""
    if not isinstance(body, dict):
        raise ValueError('新增 Agent 内容必须是对象')
    allowed = EDITABLE_MANIFEST_FIELDS | {'key', 'content', 'enabled'}
    unknown = set(body) - allowed
    if unknown:
        raise ValueError(f'不支持创建字段：{sorted(unknown)[0]}')
    key = _safe_key(body.get('key', ''))
    directory = AGENTS_DIR / key
    if directory.exists():
        raise ValueError('Agent 已存在')
    content = body.get('content', '')
    if not isinstance(content, str) or not content.strip():
        raise ValueError('内容不能为空')
    if 'enabled' in body and not isinstance(body['enabled'], bool):
        raise ValueError('enabled 必须是布尔值')

    # Build the manifest through the same field validation as updates. This
    # keeps new and existing Agents subject to one contract.
    manifest = _validate_manifest_patch(
        {field: value for field, value in body.items() if field != 'key'},
        {'key': key, 'name': '', 'version': '', 'description': '', 'skills': []},
        key,
        'agent',
    )
    if not manifest.get('name') or not manifest.get('version'):
        raise ValueError('name 和 version 不能为空')

    sync()
    conn = connect()
    try:
        if conn.execute('SELECT 1 FROM agents WHERE key=?', (key,)).fetchone():
            raise ValueError('Agent 已存在')
        _validate_agent_skill_bindings(manifest.get('skills', []), conn=conn)
    finally:
        conn.close()

    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    directory.mkdir()
    try:
        _atomic_write(directory / 'system.md', content)
        _atomic_write(directory / 'manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    except Exception:
        for path in (directory / 'system.md', directory / 'manifest.json'):
            if path.exists():
                path.unlink()
        directory.rmdir()
        raise
    sync()
    if 'enabled' in body:
        set_enabled('agent', key, body['enabled'])
    return get_detail('agent', key)


def _validate_agent_skill_bindings(skill_keys: list[str], conn=None, *, allowed_disabled: set[str] | None = None) -> None:
    """Reject new Agent bindings to missing or disabled skills.

    Existing Agents may retain a disabled binding so that disabling a Skill is
    reversible and never silently rewrites an Agent. New or edited bindings,
    however, must point at an enabled Skill.
    """
    owns_connection = conn is None
    allowed_disabled = allowed_disabled or set()
    conn = conn or connect()
    try:
        for skill_key in skill_keys:
            row = conn.execute('SELECT enabled FROM skills WHERE key=?', (skill_key,)).fetchone()
            if not row:
                raise ValueError(f'技能不存在：{skill_key}')
            if not row['enabled'] and skill_key not in allowed_disabled:
                raise ValueError(f'技能未启用：{skill_key}')
    finally:
        if owns_connection:
            conn.close()


def create_skill(body: dict) -> dict:
    """Create a skill through the same safe file-backed contract as Agents."""
    if not isinstance(body, dict):
        raise ValueError('新增技能内容必须是对象')
    body = {'version': '1.0.0', **body}
    allowed = {'key', 'name', 'version', 'description', 'content', 'enabled'}
    unknown = set(body) - allowed
    if unknown:
        raise ValueError(f'不支持创建字段：{sorted(unknown)[0]}')
    key = _safe_key(body.get('key', ''))
    directory = SKILLS_DIR / key
    if directory.exists():
        raise ValueError('技能已存在')
    content = body.get('content', '')
    if not isinstance(content, str) or not content.strip():
        raise ValueError('技能内容不能为空')
    if 'enabled' in body and not isinstance(body['enabled'], bool):
        raise ValueError('enabled 必须是布尔值')
    manifest = _validate_manifest_patch(
        {field: value for field, value in body.items() if field != 'key'},
        {'key': key, 'name': '', 'version': '', 'description': ''}, key, 'skill')
    if not manifest.get('name') or not manifest.get('version'):
        raise ValueError('name 和 version 不能为空')
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    directory.mkdir()
    try:
        _atomic_write(directory / 'SKILL.md', content)
        _atomic_write(directory / 'manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    except Exception:
        for path in (directory / 'SKILL.md', directory / 'manifest.json'):
            if path.exists():
                path.unlink()
        directory.rmdir()
        raise
    sync()
    if 'enabled' in body:
        set_enabled('skill', key, body['enabled'])
    return get_detail('skill', key)


def export_bundle() -> bytes:
    """Export only registered text/manifest files; never export runtime data."""
    _bootstrap_templates()
    memory = io.BytesIO()
    with zipfile.ZipFile(memory, 'w', zipfile.ZIP_DEFLATED) as archive:
        for kind, base, content_name in (('skills', SKILLS_DIR, 'SKILL.md'), ('agents', AGENTS_DIR, 'system.md')):
            for directory in sorted(base.glob('*')):
                if not directory.is_dir():
                    continue
                manifest = directory / 'manifest.json'
                content = directory / content_name
                if manifest.is_file() and content.is_file():
                    archive.writestr(f'{kind}/{directory.name}/manifest.json', manifest.read_text(encoding='utf-8'))
                    archive.writestr(f'{kind}/{directory.name}/{content_name}', content.read_text(encoding='utf-8'))
    return memory.getvalue()


def import_bundle(raw: bytes) -> dict:
    if len(raw) > 10 * 1024 * 1024:
        raise ValueError('Hub 压缩包不能超过 10 MB')
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ValueError('Hub 文件不是有效压缩包') from exc
    imported = {'skills': 0, 'agents': 0}
    with archive:
        names = archive.namelist()
        if any(name.startswith('/') or '..' in Path(name).parts for name in names):
            raise ValueError('Hub 压缩包含有不安全路径')
        grouped = {}
        for name in names:
            parts = Path(name).parts
            if len(parts) == 3 and parts[0] in {'skills', 'agents'}:
                grouped.setdefault((parts[0], parts[1]), {})[parts[2]] = archive.read(name)
        for (kind, key), files in grouped.items():
            _safe_key(key)
            content_name = 'SKILL.md' if kind == 'skills' else 'system.md'
            if set(files) != {'manifest.json', content_name}:
                raise ValueError(f'Hub 条目不完整：{kind}/{key}')
            manifest = json.loads(files['manifest.json'].decode('utf-8'))
            if manifest.get('key') != key:
                raise ValueError(f'Hub manifest key 不匹配：{key}')
            body = {**manifest, 'content': files[content_name].decode('utf-8')}
            if kind == 'skills':
                create_skill(body) if not (SKILLS_DIR / key).exists() else update('skill', key, {k: v for k, v in body.items() if k != 'key'})
            else:
                create_agent(body) if not (AGENTS_DIR / key).exists() else update('agent', key, {k: v for k, v in body.items() if k != 'key'})
            imported[kind] += 1
    return {'ok': True, **imported}


def list_revisions(kind: str, key: str) -> list[dict]:
    _kind_config(kind)
    key = _safe_key(key)
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(
            '''SELECT id,kind,key,version,manifest_json,content,created_at
               FROM hub_revisions WHERE kind=? AND key=? ORDER BY id DESC''', (kind, key)
        )]
    finally:
        conn.close()


def restore_revision(revision_id: int) -> dict:
    conn = connect()
    try:
        row = conn.execute('SELECT * FROM hub_revisions WHERE id=?', (revision_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise ValueError('Hub 版本不存在')
    kind, key = row['kind'], _safe_key(row['key'])
    base, label, content_name = _kind_config(kind)
    directory = base / key
    if not directory.is_dir():
        raise ValueError(f'{label} {key} 不存在')
    manifest = json.loads(row['manifest_json'])
    content_path = directory / content_name
    manifest_path = directory / 'manifest.json'
    current = _manifest(directory, key)
    current_content = content_path.read_text(encoding='utf-8')
    conn = connect()
    try:
        conn.execute('''INSERT INTO hub_revisions(kind,key,version,manifest_json,content)
                        VALUES(?,?,?,?,?)''',
                     (kind, key, current['version'], json.dumps(current, ensure_ascii=False), current_content))
        conn.commit()
    finally:
        conn.close()
    _atomic_write(content_path, row['content'])
    _atomic_write(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    sync()
    return get_detail(kind, key)
