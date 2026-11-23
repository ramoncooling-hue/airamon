"""Data and privacy self-check for the local service (B2/T2.18a, D-10).

Read-only and redacted: never returns absolute paths, settings values, raw log
lines or database bytes. Powers GET /api/diagnostics and the redacted
diagnostics bundle (POST /api/diagnostics/export).

Everything degrades instead of raising: a missing database, log file or backup
directory still produces a useful shape for the settings panel.

The log tail is re-redacted at read time (B1.1/P2-7 formatter semantics) so a
model key in settings.json or a URL query secret can never enter the bundle.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from app import logging_setup
from app.memory import db
from app.runtime import ROOT

EXPORT_FORMAT_VERSION = 1
EXPORT_DIRNAME = 'diagnostics'
MAX_BACKUPS = 10
MAX_EXPORT_FILES = 10
MAX_LOG_TAIL_BYTES = 256 * 1024
MAX_LOG_ENTRIES = 20
MAX_LOG_LEVELS = {'ERROR', 'WARNING'}
MAX_LOG_LINE_CHARS = 300
MAX_EXPORT_JSON_BYTES = 2 * 1024 * 1024
MAX_EXPORT_BYTES = 4 * 1024 * 1024
BACKUP_SUFFIXES = ('.zip', '.db')
_LOG_LINE = re.compile(
    r'^(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) '
    r'(?P<level>ERROR|WARNING) (?P<logger>\S+) (?P<message>.*)$')


def _iso(timestamp: float | None) -> str | None:
    try:
        return datetime.fromtimestamp(float(timestamp), timezone.utc).isoformat(timespec='seconds')
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def _app_version() -> str:
    try:
        return (ROOT / 'VERSION').read_text(encoding='utf-8').strip()[:32]
    except OSError:
        return ''


# ── redaction ──────────────────────────────────────────────────────────────


def _known_secrets() -> tuple[str, ...]:
    """Environment + registered + settings.json secrets from the logging layer."""
    try:
        return tuple(logging_setup._known_secrets(data_dir=db.DATA_DIR))
    except Exception:  # degraded builds must still produce a summary
        return ()


def _redact(text) -> str:
    """Apply the log formatter's redaction rules to one untrusted string."""
    raw = str(text)
    try:
        formatter = logging_setup.RedactingFormatter(
            fmt='%(message)s', secrets=_known_secrets(),
            secret_provider=_known_secrets)
        record = logging.LogRecord('mindock.diagnostics', logging.INFO,
                                   __file__, 0, raw, None, None)
        raw = formatter.format(record)
    except Exception:  # never leak because redaction failed: drop the line
        return '[unavailable]'
    return raw[:MAX_LOG_LINE_CHARS]


def _scrub(value):
    """Defense-in-depth pass used before a diagnostic bundle is serialized."""
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, str):
        return _redact(value)
    return value


# ── individual sections ────────────────────────────────────────────────────


def data_root_summary() -> dict:
    root = Path(db.DATA_DIR).expanduser()
    custom = bool(os.environ.get('CYBERPAL_DATA_DIR', '').strip())
    try:
        exists = root.is_dir()
    except OSError:
        exists = False
    database = Path(db.DB_PATH)
    try:
        database_size = int(database.stat().st_size) if database.is_file() else None
    except OSError:
        database_size = None
    # Only the leaf name leaves the service; the absolute path never does.
    return {
        'basename': root.name or 'data',
        'custom': custom,
        'exists': exists,
        'database_present': database.is_file(),
        'database_size_bytes': database_size,
    }


def schema_summary() -> dict:
    summary = {'user_version': None, 'migrations': None,
               'latest': [], 'compatible': None}
    conn = None
    try:
        conn = db.connect()
        summary['user_version'] = int(conn.execute('PRAGMA user_version').fetchone()[0])
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if has_table:
            versions = [str(row[0]) for row in conn.execute(
                'SELECT version FROM schema_migrations ORDER BY version')]
            summary['migrations'] = len(versions)
            summary['latest'] = versions[-3:]
    except Exception:
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    try:
        from .migrations import assert_compatible
        assert_compatible(Path(db.DB_PATH), Path(db.SCHEMA_DIR))
        summary['compatible'] = True
    except Exception:
        summary['compatible'] = None if summary['migrations'] is None else False
    return summary


def capabilities_summary() -> dict:
    result = {'sqlite_version': '', 'fts5': False, 'trigram': False,
              'search_fts_table': False, 'search_fts_readable': None}
    try:
        result.update(db.sqlite_capabilities())
    except Exception:
        pass
    conn = None
    try:
        conn = db.connect()
        present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='search_fts'"
        ).fetchone()
        result['search_fts_table'] = bool(present)
        if present:
            try:
                conn.execute('SELECT COUNT(*) FROM search_fts').fetchone()
                result['search_fts_readable'] = True
            except Exception:
                result['search_fts_readable'] = False
    except Exception:
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return result


def backup_summary() -> dict:
    directory = Path(db.APP_DIR) / 'backups'
    items = []
    present = directory.is_dir()
    if present:
        try:
            candidates = [path for path in directory.iterdir()
                          if path.is_file() and path.suffix.lower() in BACKUP_SUFFIXES]
        except OSError:
            candidates = []
        for path in candidates:
            try:
                stat = path.stat()
            except OSError:
                continue
            items.append({
                'name': path.name[:120],
                'modified_at': _iso(stat.st_mtime),
                'size_bytes': int(stat.st_size),
            })
        items.sort(key=lambda item: item['modified_at'] or '', reverse=True)
        items = items[:MAX_BACKUPS]
    return {
        'directory_present': present,
        'count': len(items),
        'latest_at': items[0]['modified_at'] if items else None,
        'items': items,
    }


def _tail_lines(path: Path) -> tuple[list[str], bool]:
    try:
        size = path.stat().st_size
    except OSError:
        return [], False
    try:
        with path.open('rb') as stream:
            if size > MAX_LOG_TAIL_BYTES:
                stream.seek(size - MAX_LOG_TAIL_BYTES)
                truncated = True
            else:
                truncated = False
            raw = stream.read()
    except OSError:
        return [], False
    return raw.decode('utf-8', errors='replace').splitlines(), truncated


def log_summary() -> dict:
    path = logging_setup.log_path(db.DATA_DIR)
    summary = {
        'present': False, 'size_bytes': None, 'modified_at': None,
        'truncated': False, 'levels': {'ERROR': 0, 'WARNING': 0},
        'entries': [],
    }
    try:
        stat = path.stat()
    except OSError:
        return summary
    summary['present'] = True
    summary['size_bytes'] = int(stat.st_size)
    summary['modified_at'] = _iso(stat.st_mtime)
    lines, truncated = _tail_lines(path)
    summary['truncated'] = truncated
    entries = []
    for line in lines:
        match = _LOG_LINE.match(line)
        if not match:
            continue  # tracebacks/continuations are summarized by their head line
        level = match.group('level')
        if level in MAX_LOG_LEVELS:
            summary['levels'][level] += 1
        entries.append({
            'time': match.group('time')[:23],
            'level': level,
            'logger': match.group('logger')[:80],
            'message': _redact(match.group('message')),
        })
    summary['entries'] = entries[-MAX_LOG_ENTRIES:]
    return summary


def _count_by_workspace(conn, sql: str) -> dict:
    return {row[0]: int(row[1]) for row in conn.execute(sql).fetchall()}


def workspace_counts() -> dict:
    result = {'count': 0, 'items': []}
    conn = None
    try:
        conn = db.connect()
        workspaces = conn.execute(
            'SELECT id,key FROM workspaces ORDER BY sort_order,id').fetchall()
        kp_counts = _count_by_workspace(conn, '''
            SELECT c.workspace_id, COUNT(*) FROM kp JOIN courses c ON c.id=kp.course_id
            GROUP BY c.workspace_id''')
        document_counts = None
        has_documents = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_documents'"
        ).fetchone()
        if has_documents:
            try:
                document_counts = _count_by_workspace(conn, '''
                    SELECT workspace_id, COUNT(*) FROM knowledge_documents
                    WHERE status!='deleted' GROUP BY workspace_id''')
            except Exception:
                document_counts = None
        note_counts = None
        has_notes = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_notes'"
        ).fetchone()
        if has_notes:
            try:
                note_counts = _count_by_workspace(conn, '''
                    SELECT c.workspace_id, COUNT(*) FROM knowledge_notes n
                    JOIN source_files sf ON sf.id=n.source_id
                    JOIN courses c ON c.id=sf.course_id
                    GROUP BY c.workspace_id''')
            except Exception:
                note_counts = None
        items = []
        for row in workspaces:
            items.append({
                'workspace': str(row[1]),
                'kp': kp_counts.get(row[0], 0),
                'documents': None if document_counts is None else document_counts.get(row[0], 0),
                'notes': None if note_counts is None else note_counts.get(row[0], 0),
            })
        result = {'count': len(items), 'items': items}
    except Exception:
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return result


def recovery_hints(backups: dict, schema: dict, capabilities: dict) -> dict:
    hints = []
    if not backups.get('directory_present'):
        hints.append('未发现本机备份目录；可在桌面壳创建备份后再尝试还原')
    elif not backups.get('count'):
        hints.append('备份目录为空；可在桌面壳「备份与恢复」中创建备份')
    else:
        hints.append('最近一次备份可在桌面壳「备份与恢复」中还原')
    if schema.get('compatible') is False:
        hints.append('数据库 schema 与当前版本不一致；请先完成迁移或从备份恢复')
    if not capabilities.get('fts5'):
        hints.append('当前 SQLite 不支持 FTS5，检索将使用 LIKE 回退')
    return {
        'backup_available': bool(backups.get('count')),
        'backup_directory_present': bool(backups.get('directory_present')),
        'browser_backup_supported': False,
        'hints': hints,
    }


# ── public API ─────────────────────────────────────────────────────────────


def summary() -> dict:
    """Redacted data/privacy self-check; safe to show in the settings panel."""
    backups = backup_summary()
    schema = schema_summary()
    capabilities = capabilities_summary()
    return {
        'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'format_version': EXPORT_FORMAT_VERSION,
        'app': {'version': _app_version(), 'platform': platform.system()},
        'data_root': data_root_summary(),
        'schema': schema,
        'capabilities': capabilities,
        'backups': backups,
        'logs': log_summary(),
        'workspaces': workspace_counts(),
        'recovery': recovery_hints(backups, schema, capabilities),
    }


def _export_directory() -> Path:
    directory = Path(db.APP_DIR) / EXPORT_DIRNAME
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    return directory


def _prune_exports(directory: Path) -> None:
    try:
        archives = sorted(
            (path for path in directory.glob('diagnostics-*.zip') if path.is_file()),
            key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return
    for stale in archives[MAX_EXPORT_FILES:]:
        try:
            stale.unlink()
        except OSError:
            pass


def export() -> dict:
    """Write a redacted bundle and return its file name/size (never a path)."""
    snapshot = _scrub(summary())
    logs = {
        'present': snapshot['logs']['present'],
        'truncated': snapshot['logs']['truncated'],
        'levels': snapshot['logs']['levels'],
        'entries': snapshot['logs']['entries'],
    }
    diagnostics_bytes = json.dumps(
        {'format_version': EXPORT_FORMAT_VERSION,
         'generated_at': snapshot['generated_at'],
         'diagnostics': snapshot}, ensure_ascii=False).encode('utf-8')
    logs_bytes = json.dumps(logs, ensure_ascii=False).encode('utf-8')
    if len(diagnostics_bytes) + len(logs_bytes) > MAX_EXPORT_JSON_BYTES:
        raise ValueError('诊断数据超出大小限制')

    directory = _export_directory()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    name = 'diagnostics-%s-%s.zip' % (stamp, uuid.uuid4().hex[:8])
    target = directory / name
    temporary = directory / ('.diagnostics-%s.tmp' % uuid.uuid4().hex)
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('diagnostics.json', diagnostics_bytes)
            archive.writestr('logs-summary.json', logs_bytes)
        if temporary.stat().st_size > MAX_EXPORT_BYTES:
            raise ValueError('诊断包超出大小限制')
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    _prune_exports(directory)
    return {
        'ok': True,
        'filename': target.name,
        'size_bytes': int(target.stat().st_size),
        'created_at': snapshot['generated_at'],
        'contents': ['diagnostics.json', 'logs-summary.json'],
    }
