"""Structured, rotating file logging for the local service.

The app keeps user data under CYBERPAL_DATA_DIR; logs live in data/app/logs/.
Every line is redacted before it reaches disk: model keys, session tokens and
API secrets must never be persisted. stdlib logging only (no new dependency).

B1.1/P2-7: model keys normally live in data/app/settings.json, not in the
environment. The formatter therefore reads those values (fast/reasoning
api_key, web.api_key and model_profiles[].api_key) and scrubs them too, with a
short per-path cache so settings.json is re-read at most once per cache window.
Runtime callers may call register_secret() for keys that appear after
setup_logging() ran.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FILENAME = 'mindock.log'
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 3
REDACTED = '[redacted]'
FORMAT = '%(asctime)s %(levelname)s %(name)s %(message)s'
# How long values parsed from settings.json are reused before re-reading it.
SETTINGS_SECRETS_TTL_SECONDS = 60.0

# Secrets that must be scrubbed even when they appear without a key prefix.
_SECRET_ENV_KEYS = (
    'MINDOCK_SESSION_TOKEN',
    'MINDOCK_READY_NONCE',
    'MINDOCK_API_KEY',
    'OPENAI_API_KEY',
    'DEEPSEEK_API_KEY',
    'ANTHROPIC_API_KEY',
)

_KEY_VALUE = re.compile(
    r'(?i)((?:access[_-]?token|session[_-]?token|refresh[_-]?token|api[_-]?key|'
    r'secret|password|passwd|authorization|token)["\']?\s*[:=]\s*["\']?)'
    r'([^\s"\',;}&]{4,})')
_BEARER = re.compile(r'(?i)\b(bearer\s+)([A-Za-z0-9._~+/=-]{8,})')
# B1.1/P2-7: URL query parameters bypass the key=value rule above because its
# value pattern would swallow the following "&param=..." text. Keep the
# parameter name, replace only its value, and leave sibling parameters intact.
_URL_QUERY_SECRET = re.compile(
    r'(?i)([?&](?:key|api[_-]?key|token|access[_-]?token|secret)=)'
    r'([^&\s"\'#<>]{4,})')

_settings_cache: dict[Path, tuple[float, tuple[str, ...]]] = {}
_registered_secrets: set[str] = set()
_secrets_lock = threading.Lock()


def _data_root(data_dir: str | Path | None = None) -> Path:
    if data_dir is None:
        from .runtime import data_dir as configured_data_dir
        data_dir = configured_data_dir()
    return Path(data_dir).expanduser()


def _settings_file(data_dir: str | Path | None = None) -> Path:
    return _data_root(data_dir) / 'app' / 'settings.json'


def _collect_settings_secrets(path: Path) -> tuple[str, ...]:
    """Best-effort parse of settings.json; any problem means "no known keys"."""
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError, RecursionError):
        return ()
    if not isinstance(payload, dict):
        return ()
    values: set[str] = set()

    def take(section) -> None:
        if not isinstance(section, dict):
            return
        key = section.get('api_key')
        if isinstance(key, str) and len(key) >= 8:
            values.add(key)

    for tier in ('fast', 'reasoning'):
        take(payload.get(tier))
    take(payload.get('web'))
    profiles = payload.get('model_profiles')
    if isinstance(profiles, list):
        for item in profiles:
            take(item)
    return tuple(sorted(values, key=len, reverse=True))


def _settings_secrets(data_dir: str | Path | None = None) -> tuple[str, ...]:
    """Cached settings.json secrets (60s TTL, one cache entry per path)."""
    path = _settings_file(data_dir)
    try:
        key = path.resolve()
    except OSError:  # pragma: no cover - resolve() rarely fails
        key = path
    now = time.monotonic()
    with _secrets_lock:
        cached = _settings_cache.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]
    secrets = _collect_settings_secrets(key)
    with _secrets_lock:
        if len(_settings_cache) > 16:
            _settings_cache.clear()
        _settings_cache[key] = (now + SETTINGS_SECRETS_TTL_SECONDS, secrets)
    return secrets


def register_secret(value) -> None:
    """Register a runtime secret (for example a key saved in the settings UI).

    Values shorter than 8 characters are ignored so ordinary log text is not
    mangled. The registration applies to every subsequent log line without
    touching the disk.
    """
    if not isinstance(value, str):
        return
    value = value.strip()
    if len(value) < 8:
        return
    with _secrets_lock:
        _registered_secrets.add(value)


def refresh() -> None:
    """Drop cached settings.json secrets; the next log line re-reads the file."""
    with _secrets_lock:
        _settings_cache.clear()


def _known_secrets(environ=None,
                   data_dir: str | Path | None = None) -> tuple[str, ...]:
    environ = os.environ if environ is None else environ
    values = {environ.get(key, '') for key in _SECRET_ENV_KEYS}
    with _secrets_lock:
        values.update(_registered_secrets)
    values.update(_settings_secrets(data_dir))
    # Short values cannot be matched without mangling ordinary log text.
    return tuple(sorted((value for value in values if len(value) >= 8),
                        key=len, reverse=True))


class RedactingFormatter(logging.Formatter):
    """Formatter that removes secrets from message and traceback text."""

    def __init__(self, fmt: str = FORMAT, datefmt: str | None = None,
                 secrets: tuple[str, ...] = (), secret_provider=None):
        super().__init__(fmt, datefmt)
        self._secrets = tuple(secrets)
        # Optional callable so secrets that appear after setup_logging() are
        # still redacted without rebuilding the handler.
        self._secret_provider = secret_provider

    def _effective_secrets(self) -> list[str]:
        values = list(self._secrets)
        if self._secret_provider is not None:
            try:
                dynamic = self._secret_provider()
            except Exception:  # never let logging fail because of redaction
                dynamic = ()
            for value in dynamic:
                if value not in values:
                    values.append(value)
        return values

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self._effective_secrets():
            text = text.replace(secret, REDACTED)
        text = _BEARER.sub(lambda match: match.group(1) + REDACTED, text)
        text = _URL_QUERY_SECRET.sub(lambda match: match.group(1) + REDACTED, text)
        text = _KEY_VALUE.sub(lambda match: match.group(1) + REDACTED, text)
        return text


class _PrivateRotatingFileHandler(RotatingFileHandler):
    """Rotating handler that keeps 0600 after every rollover.

    RotatingFileHandler re-opens the live file with the process umask after a
    rollover, which would silently widen the local log to 0644. Re-apply the
    private mode on every open so rotation cannot weaken O-OBS-1.
    """

    def _open(self):
        stream = super()._open()
        try:
            os.chmod(self.baseFilename, 0o600)
        except OSError:  # pragma: no cover - best effort on odd filesystems
            pass
        return stream


def log_path(data_dir: str | Path | None = None) -> Path:
    return Path(_data_root(data_dir)) / 'app' / 'logs' / LOG_FILENAME


def setup_logging(data_dir: str | Path | None = None,
                  level: int = logging.INFO) -> Path | None:
    """Install the redacting rotating file handler once and return its path.

    Best effort: an unwritable data directory must not stop the companion app.
    Calling it again replaces the previous handler instead of duplicating lines.
    """
    path = log_path(data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        handler = _PrivateRotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding='utf-8')
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        return None

    configured_dir = data_dir
    handler.setFormatter(RedactingFormatter(
        secrets=_known_secrets(data_dir=configured_dir),
        secret_provider=lambda: _known_secrets(data_dir=configured_dir)))
    handler._mindock_owned = True  # type: ignore[attr-defined]
    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, '_mindock_owned', False):
            root.removeHandler(existing)
            existing.close()
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)
    return path
