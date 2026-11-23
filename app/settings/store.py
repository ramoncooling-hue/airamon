"""CyberPal · 模型与接口配置。

存在 `data/app/settings.json`（不进版本库），只放本机。
密钥不回传前端明文——只回「有没有配」和尾号。
"""

from __future__ import annotations

import json
import hashlib
import os
import stat
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..memory.db import APP_DIR

SETTINGS_PATH = APP_DIR / "settings.json"
MAX_SETTINGS = 4 * 1024 * 1024
_LOCK = threading.RLock()


class RevisionConflict(ValueError):
    pass

DEFAULTS: dict[str, Any] = {
    # 两个档位：fast 用于日常问答，reasoning 用于论文/论证
    "fast": {
        "provider": "",
        "base_url": "",
        "model": "",
        "api_key": "",
    },
    "reasoning": {
        "provider": "",
        "base_url": "",
        "model": "",
        "api_key": "",
    },
    "model_profiles": [],
    "model_routes": {"fast": "", "reasoning": ""},
    # 检索与回答的行为开关
    "retrieval": {
        "top_k": 6,
        "require_citation": True,
        "vector": "off",          # off / local / remote
        "vector_model": "",
    },
    "web": {"provider": "bing_rss", "api_key": ""},
    "local_sources": {},
    # 外部通道
    "channels": {
        "tsinghua_imap_host": "mails.tsinghua.edu.cn",
        "tsinghua_imap_port": 993,
        "tsinghua_user": "",
        "tsinghua_password": "",
    },
}


def _merge(base: dict, over: dict) -> dict:
    """浅合并两层，保持 DEFAULTS 里新增的键不会因旧文件而缺失。"""
    out = json.loads(json.dumps(base))
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def _json_object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('Duplicate configuration key')
        out[key] = value
    return out


def _invalid_constant(value):
    raise ValueError('Invalid JSON constant')


def _check_path():
    path = Path(SETTINGS_PATH).absolute()
    for item in (*reversed(path.parents), path):
        if item.is_symlink():
            raise ValueError('Settings path cannot contain symlinks')
    if path.exists() and not stat.S_ISREG(path.stat().st_mode):
        raise ValueError('Settings must be a regular file')
    return path


def _validate(cfg):
    from . import model_profiles

    if not isinstance(cfg, dict):
        raise ValueError('Invalid settings object')
    for section, default in DEFAULTS.items():
        if isinstance(default, dict) and not isinstance(cfg.get(section, default), dict):
            raise ValueError('Invalid settings section')
    model_profiles.validate(cfg.get('model_profiles', []),
                            cfg.get('model_routes', {'fast': '', 'reasoning': ''}))


def _load():
    path = _check_path()
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    except FileNotFoundError:
        return _merge(DEFAULTS, {}), hashlib.sha256(b'absent').hexdigest()
    with os.fdopen(fd, 'rb') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError('Settings must be a regular file')
        raw = handle.read(MAX_SETTINGS + 1)
    if len(raw) > MAX_SETTINGS:
        raise ValueError('Settings exceed size limit')
    try:
        saved = json.loads(raw, object_pairs_hook=_json_object, parse_constant=_invalid_constant)
        _validate(saved)
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise ValueError('Settings file is invalid; repair it before saving') from None
    return _merge(DEFAULTS, saved), hashlib.sha256(raw).hexdigest()


@contextmanager
def _transaction():
    # All settings writers share this lock, including separate backend processes.
    with _LOCK:
        path = _check_path()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _check_path()
        lock_path = path.with_name('.settings.lock')
        if lock_path.is_symlink():
            raise ValueError('Settings lock cannot be a symlink')
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'r+b') as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError('Invalid settings lock')
            if os.name == 'nt':
                import msvcrt
                if os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b'\0')
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == 'nt':
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _save(cfg, revision):
    """Call inside _transaction; no live file is touched until replacement."""
    _validate(cfg)
    raw = json.dumps(cfg, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
    if len(raw) > MAX_SETTINGS:
        raise ValueError('Settings exceed size limit')
    path = _check_path()
    fd, temporary = tempfile.mkstemp(prefix='.settings-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            os.chmod(temporary, 0o600)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        _check_path()
        if _load()[1] != revision:
            raise RevisionConflict('Settings changed; reload before saving')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    # A model key can change at runtime. Drop the log-redaction cache so the
    # next log line re-reads settings.json and the new secret is scrubbed
    # immediately instead of after the 60s cache TTL.
    try:
        from ..logging_setup import refresh as _refresh_log_secrets
        _refresh_log_secrets()
    except Exception:  # pragma: no cover - redaction must never block settings
        pass
    return hashlib.sha256(raw).hexdigest()


def read_raw() -> dict:
    with _LOCK:
        return _load()[0]


def _mask(secret: str) -> str:
    """只露尾 4 位，前端不接触明文密钥。"""
    if not secret:
        return ""
    return "••••••" + secret[-4:] if len(secret) > 4 else "••••••"


def read_public() -> dict:
    """给前端看的版本：密钥脱敏，只说明配置状态。"""
    return _public(read_raw())


def _public(cfg) -> dict:
    from .model_profiles import public_profiles

    out = json.loads(json.dumps(cfg))
    out['model_profiles'] = public_profiles(cfg.get('model_profiles', []))
    for tier in ("fast", "reasoning"):
        key = out.get(tier, {}).get("api_key", "")
        out[tier]["api_key_set"] = bool(key)
        out[tier]["api_key"] = _mask(key)
    ch = out.get("channels", {})
    pwd = ch.get("tsinghua_password", "")
    ch["password_set"] = bool(pwd)
    ch["tsinghua_password"] = _mask(pwd)
    for section in ("web",):
        key = out.get(section, {}).get("api_key", "")
        out[section]["api_key_set"] = bool(key)
        out[section]["api_key"] = _mask(key)
        provider = out[section].get("provider") or "bing_rss"
        out[section]["requires_key"] = provider in {"bocha", "tavily"}
        out[section]["ready"] = provider == "bing_rss" or bool(key)
        out[section]["status"] = "免 Key 可用" if provider == "bing_rss" else ("已配置" if key else "需要 API Key")
    return out


def write(patch: dict) -> dict:
    """接受局部更新。空字符串 = 清除该字段；不传 = 保持原样。"""
    if not isinstance(patch, dict) or {'model_profiles', 'model_routes'} & patch.keys():
        raise ValueError('Use the revision-checked models endpoint for model profiles')
    with _transaction():
        current, revision = _load()
        for section, values in patch.items():
            if isinstance(values, dict) and isinstance(current.get(section), dict):
                for k, v in values.items():
                    if isinstance(v, str) and v.startswith("••••"):
                        continue
                    current[section][k] = v
            else:
                current[section] = values
        public = _public(current)
        _save(current, revision)
        return public


def configured() -> dict:
    """哪些档位真的可用 —— 前端据此决定显不显示「已接入」标记。"""
    from .model_profiles import resolve

    result = {'fast': False, 'reasoning': False}
    try:
        cfg = read_raw()
        for tier in result:
            selected = resolve(cfg, tier)
            result[tier] = bool(selected.get('base_url') and selected.get('model'))
    except (ValueError, TypeError, KeyError, OSError, RecursionError):
        return {'fast': False, 'reasoning': False}
    return result
