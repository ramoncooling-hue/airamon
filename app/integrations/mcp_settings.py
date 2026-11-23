"""Local executable configuration: private atomic writes and revision checks."""
import hashlib
import importlib.util
import json
import os
import stat
import tempfile
import threading

from app.memory import db, repo
from . import mcp_bridge as bridge

_LOCK = threading.RLock()


class RevisionConflict(ValueError):
    pass


def config_path():
    return bridge.config_path()


def _check_path():
    path = config_path()
    for item in (db.APP_DIR, path.parent, path):
        if item.is_symlink():
            raise ValueError('MCP 配置路径不能是符号链接')
    if path.exists() and not stat.S_ISREG(path.stat().st_mode):
        raise ValueError('MCP 配置必须是普通文件')
    return path


def validate(servers):
    bridge.validate_servers(servers)
    for server in servers:
        workspace = server.get('workspace')
        if not repo.get_workspace(workspace):
            raise ValueError('请选择存在的工作区')
        course = server.get('course')
        if course is not None and course not in {c['name'] for c in repo.list_courses(workspace)}:
            raise ValueError('请选择该工作区内的课程')


def _load():
    path = _check_path()
    if not path.exists():
        return [], hashlib.sha256(b'absent').hexdigest()
    if path.stat().st_size > bridge.MAX_CONFIG:
        raise ValueError('MCP 配置超出大小限制')
    raw = path.read_bytes()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {'servers'}:
            raise ValueError()
        bridge.validate_servers(data['servers'])
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise ValueError('MCP 配置文件损坏或格式无效，请先修复原文件') from None
    return data['servers'], hashlib.sha256(raw).hexdigest()


def read():
    with _LOCK:
        servers, revision = _load()
        missing = {}
        for server in servers:
            names = list(server.get('env_names', []))
            if server.get('bearer_env'):
                names.append(server['bearer_env'])
            missing[server['id']] = sorted({name for name in names if not os.environ.get(name)})
        return {'servers': servers, 'revision': revision,
                'sdk_installed': importlib.util.find_spec('mcp') is not None,
                'capabilities': {'transports': ['stdio', 'streamable_http'],
                                 'oauth': False, 'read_only': True},
                'missing_env': missing, 'error': ''}


def save(servers, revision):
    validate(servers)
    raw = json.dumps({'servers': servers}, ensure_ascii=False, indent=2).encode('utf-8')
    if len(raw) > bridge.MAX_CONFIG:
        raise ValueError('MCP 配置超出大小限制')
    with _LOCK:
        _, current = _load()
        if not isinstance(revision, str) or revision != current:
            raise RevisionConflict('配置已被其他页面修改，请重新加载后再保存')
        path = _check_path()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _check_path()
        fd, temporary = tempfile.mkstemp(prefix='.mcp-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as handle:
                os.chmod(temporary, 0o600)
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return read()
