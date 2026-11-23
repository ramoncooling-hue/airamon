"""Checksummed SQLite upgrades with atomic SQL and restart recovery.

Versions are full filenames, preserving the historical 010/010b ordering.
The legacy inline ALTERs were consolidated into 023_legacy_schema_consolidation
(O-DATA-4); their immutable 018z identity is still recorded for databases that
already carry it. Optional SQLite features (FTS5/trigram) are probed before
011 so an FTS5-less build degrades to LIKE search instead of failing startup.
"""

import hashlib
from functools import lru_cache
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import uuid

from app.runtime import ROOT
from .files import fsync_dir, integrity, sha256, sqlite_snapshot, write_json
from .lock import runtime_lock, checked_root, owned_by_process

LEGACY_VERSION = '018z_legacy_inline_v1'
LEGACY_CHECKSUM = hashlib.sha256(b'MinDock legacy inline schema v1: workspaces, chapters, sources, relations, events').hexdigest()

# O-DATA-4: 011 creates a virtual table. SQLite builds without FTS5 cannot run
# it, so the engine probes first and records a degraded marker instead of
# rolling back the whole upgrade transaction (and startup).
FTS_SCHEMA_FILE = '011_search_index.sql'

logger = logging.getLogger(__name__)


def probe_capabilities() -> dict:
    """Probe the SQLite build for optional features (FTS5 and trigram)."""
    from app.memory.db import sqlite_capabilities
    return dict(sqlite_capabilities())


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _record_capabilities(conn, capabilities: dict) -> None:
    """Persist the probed build features and 011 status for diagnosability."""
    if not _table_exists(conn, 'schema_capabilities'):
        return
    # The probe wins over stale tables: a build without FTS5 can still carry a
    # leftover search_fts table from an earlier run. Reporting 'fts5' there
    # would mislead the diagnostics view (P3 / B2 review).
    if not capabilities.get('fts5'):
        fts_mode = 'skipped'
    elif _table_exists(conn, 'search_fts'):
        fts_mode = 'fts5'
    else:
        fts_mode = 'missing'
    rows = [
        ('sqlite', str(capabilities.get('sqlite_version') or '')),
        ('fts5', 'available' if capabilities.get('fts5') else 'unavailable'),
        ('fts5_trigram', 'available' if capabilities.get('trigram') else 'unavailable'),
        ('search_fts', fts_mode),
    ]
    conn.execute('DELETE FROM schema_capabilities')
    conn.executemany(
        'INSERT INTO schema_capabilities(name, detail) VALUES(?,?)', rows)


def _ensure_fts_schema(conn, schema_dir, capabilities: dict, records) -> bool:
    """Replay 011 when a database was first upgraded on an FTS5-less build."""
    if not capabilities.get('fts5') or FTS_SCHEMA_FILE not in records:
        return False
    if _table_exists(conn, 'search_fts'):
        return False
    sql_text = (Path(schema_dir) / FTS_SCHEMA_FILE).read_text(encoding='utf-8')
    execute_sql(conn, sql_text)
    return True


class SchemaCompatibilityError(RuntimeError):
    pass


class SchemaUpgradeError(RuntimeError):
    def __init__(self, backup):
        self.backup_path = str(backup) if backup else None
        super().__init__('Schema upgrade failed; previous database restored. Recovery snapshot: '
                         + (self.backup_path or '(new database rolled back)'))


def _catalog(schema_dir):
    files = sorted(Path(schema_dir).glob('*.sql'))
    if not files:
        raise SchemaCompatibilityError('No packaged schema files found')
    signature = tuple((str(path.resolve()), path.stat().st_mtime_ns, path.stat().st_ctime_ns,
                       path.stat().st_size) for path in files)
    result = dict(_cached_checksums(signature))
    result[LEGACY_VERSION] = LEGACY_CHECKSUM
    return files, result


@lru_cache(maxsize=16)
def _cached_checksums(signature):
    return tuple((Path(path).name, sha256(path)) for path, *_ in signature)


def _backfill_if_current(path, schema_dir, seed_backfill):
    assert_no_pending_upgrade(path)
    assert_compatible(path, schema_dir)
    _, expected = _catalog(schema_dir)
    conn = sqlite3.connect(path)
    try:
        if set(_records(conn)) != set(expected):
            return False
        with conn:
            seed_backfill(conn)
        return True
    finally:
        conn.close()


def _records(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone():
        return {}
    return dict(conn.execute('SELECT version,checksum FROM schema_migrations'))


def assert_compatible(path, schema_dir=None):
    """Read-only guard, called before any connection changes journal mode."""
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return
    files, expected = _catalog(schema_dir or ROOT / 'app/schema')
    maximum = max(int(re.match(r'\d+', p.name)[0]) for p in files)
    conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        if conn.execute('PRAGMA user_version').fetchone()[0] > maximum:
            raise SchemaCompatibilityError('Database schema is newer than this application; refusing writes')
        for version, checksum in _records(conn).items():
            if version not in expected:
                raise SchemaCompatibilityError(f'Unsupported newer schema: {version}')
            if checksum != expected[version]:
                raise SchemaCompatibilityError(f'Schema checksum mismatch: {version}')
    finally:
        conn.close()


def _journal(path):
    return Path(path).with_name('.schema-upgrade.json')


def assert_no_pending_upgrade(path):
    if _journal(path).exists():
        raise SchemaCompatibilityError('Interrupted schema upgrade; run apply_schema before opening the database')
    if Path(path).with_name('.restore-journal.json').exists():
        raise SchemaCompatibilityError('Interrupted restore; acquire runtime_lock before opening the database')


def recover_upgrade(path):
    """Under runtime_lock, before all database connections and worker startup."""
    path = Path(path)
    journal = _journal(path)
    if not journal.exists():
        return None
    state = json.loads(journal.read_text(encoding='utf-8'))
    if state.get('database') != path.name:
        raise SchemaCompatibilityError('Upgrade recovery journal belongs to another database')
    backup_name = state.get('backup')
    backup = None
    if backup_name:
        if Path(backup_name).name != backup_name or not backup_name.startswith('before-upgrade-'):
            raise SchemaCompatibilityError('Invalid upgrade recovery snapshot path')
        backup = path.parent / 'backups' / backup_name
        if backup.is_symlink() or sha256(backup) != state['sha256']:
            raise SchemaCompatibilityError('Upgrade recovery snapshot checksum mismatch')
        integrity(backup)
        temp = path.with_name('.maintenance-recover-' + uuid.uuid4().hex)
        shutil.copy2(backup, temp)
        with temp.open('rb') as stream:
            os.fsync(stream.fileno())
        for suffix in ('-wal', '-shm', '-journal'):
            Path(str(path) + suffix).unlink(missing_ok=True)
        os.replace(temp, path)
    else:
        for suffix in ('-wal', '-shm', '-journal'):
            Path(str(path) + suffix).unlink(missing_ok=True)
        path.unlink(missing_ok=True)
    fsync_dir(path.parent)
    journal.unlink()
    fsync_dir(path.parent)
    return backup


IDEMPOTENT_ALTER_MARKER = '-- @idempotent-alter'
_ALTER_ADD_COLUMN = re.compile(
    r'(?is)\A(?:\s*--[^\n]*\n|\s)*ALTER\s+TABLE\s+"?([A-Za-z_]\w*)"?\s+'
    r'ADD\s+COLUMN\s+"?([A-Za-z_]\w*)"?'
)


def _column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return any(row[1] == column for row in rows)


def _execute_statement(conn, statement: str, *, idempotent_alter: bool) -> None:
    if idempotent_alter:
        match = _ALTER_ADD_COLUMN.match(statement)
        # SQLite has no ALTER TABLE ... ADD COLUMN IF NOT EXISTS. Skip the
        # statement when a copy already carries the column (for example a
        # development database whose migration record was lost).
        if match and _column_exists(conn, match.group(1), match.group(2)):
            return
    conn.execute(statement)


def execute_sql(conn, sql, *, idempotent_alter: bool = False):
    """Use SQLite's statement parser, retaining triggers and the outer transaction.

    executescript commits an open transaction, so it cannot be used here.
    """
    pending = ''
    for char in sql:
        pending += char
        if char == ';' and sqlite3.complete_statement(pending):
            _execute_statement(conn, pending, idempotent_alter=idempotent_alter)
            pending = ''
    if pending.strip():
        _execute_statement(conn, pending, idempotent_alter=idempotent_alter)


def upgrade_database(path, schema_dir=None, *, legacy_upgrade=None, seed_backfill=None):
    """Upgrade before serving. Returns applied versions and retained backup path.

    Prefer app.memory.db.apply_schema(), which supplies legacy ALTER/backfill
    callbacks. Holding runtime_lock around service startup is supported.
    """
    path = checked_root(path)
    schema_dir = Path(schema_dir or ROOT / 'app/schema')
    if legacy_upgrade is None or seed_backfill is None:
        from app.memory.db import _legacy_upgrade, _seed_backfill
        legacy_upgrade = legacy_upgrade or _legacy_upgrade
        seed_backfill = seed_backfill or _seed_backfill
    # A settings refresh runs on an HTTP worker thread while the main thread
    # owns the lifetime lock. No DDL is allowed there, only current-schema DML.
    if (owned_by_process(path.parent.parent) and not _journal(path).exists()
            and _backfill_if_current(path, schema_dir, seed_backfill)):
        return {'applied': [], 'backup': None}
    with runtime_lock(path.parent.parent):
        recover_upgrade(path)
        assert_compatible(path, schema_dir)
        capabilities = probe_capabilities()
        skip_fts = not capabilities.get('fts5')
        files, expected = _catalog(schema_dir)
        conn = sqlite3.connect(path)
        try:
            records = _records(conn)
            existing = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone())
        finally:
            conn.close()
        pending = [p for p in files if p.name not in records]
        needs_legacy = LEGACY_VERSION not in records
        if not pending and not needs_legacy:
            conn = sqlite3.connect(path)
            healed = False
            try:
                with conn:
                    healed = _ensure_fts_schema(conn, schema_dir, capabilities, records)
                    _record_capabilities(conn, capabilities)
                    seed_backfill(conn)
            finally:
                conn.close()
            return {'applied': [FTS_SCHEMA_FILE] if healed else [], 'backup': None}
        backup = None
        if existing:
            directory = path.parent / 'backups'
            checked_root(directory)
            directory.mkdir(mode=0o700, exist_ok=True)
            backup = directory / ('before-upgrade-' + uuid.uuid4().hex + '.db')
            sqlite_snapshot(path, backup)
            with backup.open('rb') as stream:
                os.fsync(stream.fileno())
            fsync_dir(directory)
        write_json(_journal(path), {'database': path.name,
                                   'backup': backup.name if backup else None,
                                   'sha256': sha256(backup) if backup else None})
        conn = None
        applied = []
        try:
            conn = sqlite3.connect(path)
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('''CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY, checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
            for file in pending:
                if file.name == FTS_SCHEMA_FILE and skip_fts:
                    # 011 needs FTS5. Keep its migration record (so startup is
                    # stable and no backup is produced on every boot) but skip
                    # the DDL; schema_capabilities records the degraded mode and
                    # _ensure_fts_schema replays 011 if a later build has FTS5.
                    logger.warning(
                        'FTS5 unavailable; skipping %s and using LIKE fallback',
                        FTS_SCHEMA_FILE)
                    conn.execute(
                        'INSERT INTO schema_migrations(version,checksum) VALUES(?,?)',
                        (file.name, expected[file.name]))
                    applied.append(file.name)
                    continue
                sql_text = file.read_text(encoding='utf-8')
                execute_sql(conn, sql_text,
                            idempotent_alter=IDEMPOTENT_ALTER_MARKER in sql_text)
                conn.execute('INSERT INTO schema_migrations(version,checksum) VALUES(?,?)',
                             (file.name, expected[file.name]))
                applied.append(file.name)
            if needs_legacy:
                legacy_upgrade(conn)
                conn.execute('INSERT INTO schema_migrations(version,checksum) VALUES(?,?)',
                             (LEGACY_VERSION, LEGACY_CHECKSUM))
                applied.append(LEGACY_VERSION)
            _ensure_fts_schema(conn, schema_dir, capabilities, records)
            _record_capabilities(conn, capabilities)
            seed_backfill(conn)
            maximum = max(int(re.match(r'\d+', p.name)[0]) for p in files)
            conn.execute(f'PRAGMA user_version={maximum}')
            if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('Upgraded database failed integrity check')
            conn.commit()
            conn.close()
            conn = None
            _journal(path).unlink()
            fsync_dir(path.parent)
        except BaseException as exc:
            if conn is not None:
                conn.rollback()
                conn.close()
            recover_upgrade(path)
            if not isinstance(exc, Exception):
                raise
            raise SchemaUpgradeError(backup) from exc
        return {'applied': applied, 'backup': str(backup) if backup else None}
