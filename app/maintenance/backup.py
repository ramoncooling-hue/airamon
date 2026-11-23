"""Private local recovery archives, not shareable exports.

Archives contain personal data. Model/channel credentials in settings.json are
excluded. Original files are retained privately during replacement recovery.
"""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import tempfile
import unicodedata
import uuid
import zipfile

from app.runtime import ROOT
from .files import (DB_REL, excluded, fsync_dir, integrity, inventory, relocate,
                    require_space, sha256, snapshot_tree, sync_tree, write_json,
                    DatabaseIntegrityError)
from .lock import checked_root, runtime_lock, check_legacy_desktop, owned_by_process, RuntimeBusyError

FORMAT_VERSION = 1
MAX_FILES = 100_000
MAX_EXPANDED_BYTES = 16 * 1024 ** 3
MAX_FILE_BYTES = 4 * 1024 ** 3
MAX_MANIFEST_BYTES = 4 * 1024 ** 2
JOURNAL = '.restore-journal.json'
DB_SIDECARS = {str(DB_REL) + suffix for suffix in ('-wal', '-shm', '-journal')}


def redact(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = ''.join(c for c in key.lower() if c.isalnum())
            secret = any(word in normalized for word in
                         ('apikey', 'password', 'secret', 'token', 'authorization', 'cookie', 'credential'))
            result[key] = '' if secret else redact(item)
        return result
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def _redact_settings(stage):
    settings = stage / 'app/settings.json'
    if settings.exists():
        # Fail closed on malformed settings; never silently export raw secrets.
        value = json.loads(settings.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError('Invalid settings object')
        settings.write_text(json.dumps(redact(value), ensure_ascii=False, indent=2), encoding='utf-8')


def _schema_version(path):
    conn = sqlite3.connect(path)
    try:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone()
        return [r[0] for r in conn.execute('SELECT version FROM schema_migrations ORDER BY version')] if exists else []
    finally:
        conn.close()


def _backup_locked(root, archive):
    archive = checked_root(archive)
    if archive.exists() or archive.is_symlink():
        raise FileExistsError(f'Archive already exists: {archive}')
    if archive.is_relative_to(root) and not archive.is_relative_to(root / 'app/backups'):
        raise ValueError('Place backups outside the runtime or in app/backups')
    archive.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.maintenance-backup-', dir=archive.parent) as temporary:
        stage = Path(temporary) / 'data'
        stage.mkdir(mode=0o700)
        snapshot_tree(root, stage)
        _redact_settings(stage)
        files = inventory(stage)
        manifest = {
            'format_version': FORMAT_VERSION, 'kind': 'local-recovery',
            'app_version': (ROOT / 'VERSION').read_text().strip(),
            'schema_version': _schema_version(stage / DB_REL),
            'created_at': datetime.now(timezone.utc).isoformat(),
            'source_root': str(root), 'secrets_included': False,
            'files': [{'path': p.as_posix(), 'size': (stage / p).stat().st_size,
                       'sha256': sha256(stage / p)} for p in files],
            'directories': sorted(p.relative_to(stage).as_posix() for p in stage.rglob('*') if p.is_dir()),
        }
        packed = Path(temporary) / 'archive.zip'
        with zipfile.ZipFile(packed, 'w', compression=zipfile.ZIP_DEFLATED, allowZip64=True) as z:
            z.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False))
            for relative in files:
                z.write(stage / relative, 'data/' + relative.as_posix())
        packed.chmod(0o600)
        with packed.open('rb') as stream:
            os.fsync(stream.fileno())
        os.link(packed, archive)  # no overwrite even if a competing writer races us
        fsync_dir(archive.parent)
    return {'ok': True, 'archive': str(archive), 'manifest': manifest,
            'secrets_included': False, 'requires_key_reentry': True}


def backup_runtime(root, archive, *, allow_desktop_lock=False):
    """Stop backend first; only native maintenance opts into a held shell lock."""
    if owned_by_process(root):
        raise RuntimeBusyError('Stop the backend before maintenance')
    with check_legacy_desktop(root, allow_desktop_lock=allow_desktop_lock):
        with runtime_lock(root) as root:
            from .migrations import recover_upgrade
            recover_upgrade(root / DB_REL)
            return _backup_locked(root, archive)


def _relative(name):
    if not isinstance(name, str) or not name or '\\' in name or ':' in name or '\x00' in name:
        raise ValueError('Unsafe archive path')
    parts = name.split('/')
    if any(part in {'', '.', '..'} for part in parts) or PurePosixPath(name).is_absolute():
        raise ValueError('Unsafe archive path')
    path = Path(*parts)
    canonical = Path(*(unicodedata.normalize('NFD', part).casefold() for part in parts))
    if canonical.parts[0] == 'app' and parts[0] != 'app':
        raise ValueError('Runtime app directory must use canonical lowercase spelling')
    if len(parts) > 1 and canonical.parts[0] == 'app':
        reserved = {'settings.json', 'banxue.db'}
        if canonical.parts[1] in reserved and parts[1] != canonical.parts[1]:
            raise ValueError('Reserved runtime file has a noncanonical spelling')
    if excluded(path) or excluded(canonical):
        raise ValueError('Archive contains runtime control or transient files')
    return path


class _PathRegistry:
    """Check every implicit parent, not just complete archive member names."""

    def __init__(self):
        self.nodes = {('app',): (('app',), 'directory')}

    def add(self, path, kind):
        parts = Path(path).parts
        for length in range(1, len(parts) + 1):
            spelling = parts[:length]
            key = tuple(unicodedata.normalize('NFD', part).casefold() for part in spelling)
            node_kind = kind if length == len(parts) else 'directory'
            previous = self.nodes.get(key)
            if previous is not None and previous != (spelling, node_kind):
                raise ValueError('Case/Unicode alias or file/directory collision in runtime paths')
            self.nodes[key] = (spelling, node_kind)


def _extract_verified(archive, stage):
    with zipfile.ZipFile(archive) as z:
        infos = z.infolist()
        if len(infos) > MAX_FILES + 1:
            raise ValueError('Too many archive entries')
        names, folded = set(), set()
        registry = _PathRegistry()
        for info in infos:
            name = info.filename
            key = unicodedata.normalize('NFC', name).casefold()
            mode = stat.S_IFMT(info.external_attr >> 16)
            if name in names or key in folded or mode not in {0, stat.S_IFREG}:
                raise ValueError('Duplicate or non-regular archive entry')
            if info.flag_bits & 1 or info.file_size > MAX_FILE_BYTES:
                raise ValueError('Encrypted or oversized archive entry')
            if name != 'manifest.json':
                if not name.startswith('data/'):
                    raise ValueError('Unknown archive member')
                registry.add(_relative(name[5:]), 'file')
            names.add(name)
            folded.add(key)
        if 'manifest.json' not in names or z.getinfo('manifest.json').file_size > MAX_MANIFEST_BYTES:
            raise ValueError('Missing or oversized manifest')
        total = sum(info.file_size for info in infos)
        if total > MAX_EXPANDED_BYTES:
            raise ValueError('Archive exceeds expansion limit')
        require_space(stage.parent, total * 2)
        manifest = json.loads(z.read('manifest.json'))
        if (manifest.get('format_version') != FORMAT_VERSION or manifest.get('kind') != 'local-recovery'
                or manifest.get('secrets_included') is not False):
            raise ValueError('Unsupported backup format')
        entries = manifest.get('files')
        if not isinstance(entries, list) or len(entries) > MAX_FILES:
            raise ValueError('Invalid file manifest')
        directories = manifest.get('directories', [])
        if not isinstance(directories, list) or len(directories) > MAX_FILES:
            raise ValueError('Invalid directory manifest')
        # Validate all explicit directory aliases before writing any member.
        for name in directories:
            registry.add(_relative(name), 'directory')
        expected = set()
        for entry in entries:
            relative = _relative(entry['path'])
            name = 'data/' + relative.as_posix()
            if name in expected or name not in names:
                raise ValueError('Manifest member mismatch')
            expected.add(name)
            info = z.getinfo(name)
            if type(entry['size']) is not int or entry['size'] != info.file_size:
                raise ValueError('Archive size mismatch')
            destination = stage / relative
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with z.open(info) as source, destination.open('xb') as output:
                destination.chmod(0o600)
                count = 0
                while block := source.read(1024 * 1024):
                    count += len(block)
                    if count > info.file_size:
                        raise ValueError('Archive expansion exceeded declared size')
                    output.write(block)
            if count != entry['size'] or sha256(destination) != entry['sha256']:
                raise ValueError('Archive checksum mismatch')
        if names != expected | {'manifest.json'} or 'data/app/banxue.db' not in expected:
            raise ValueError('Unlisted files or missing database')
        for name in directories:
            (stage / _relative(name)).mkdir(mode=0o700, parents=True, exist_ok=True)
    integrity(stage / DB_REL)
    if manifest.get('schema_version') != _schema_version(stage / DB_REL):
        raise ValueError('Schema manifest mismatch')
    from .migrations import assert_compatible
    assert_compatible(stage / DB_REL)
    _redact_settings(stage)
    return manifest


def _archive_digest(stream):
    stream.seek(0)
    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    stream.seek(0)
    return digest


def inspect_archive(archive):
    """Read-only validation for native confirmation; never opens the live root.

    Return a bounded summary. Pass archive_sha256 to restore_runtime to bind
    confirmation to the inspected archive, including if its path is replaced.
    """
    with Path(archive).expanduser().open('rb') as stream:
        digest = _archive_digest(stream)
        with tempfile.TemporaryDirectory(prefix='.maintenance-inspect-') as temporary:
            stage = Path(temporary).resolve()
            manifest = _extract_verified(stream, stage)
            if _archive_digest(stream) != digest:
                raise ValueError('Archive changed during inspection')
            conn = sqlite3.connect(stage / DB_REL)
            try:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                counts = {name: conn.execute(f'SELECT count(*) FROM {name}').fetchone()[0]
                          if name in tables else 0 for name in ('courses', 'kp', 'events')}
            finally:
                conn.close()
    return {'ok': True, 'created_at': str(manifest.get('created_at', ''))[:80],
            'app_version': str(manifest.get('app_version', ''))[:80],
            'file_count': len(manifest['files']),
            'total_bytes': sum(entry['size'] for entry in manifest['files']),
            **counts, 'secrets_included': False, 'requires_key_reentry': True,
            'archive_sha256': digest, 'sha256': digest}


def _payload(root, registry=None):
    registry = registry or _PathRegistry()
    entries = []
    for child in root.iterdir():
        # Include control names in the spelling registry, but do not move them.
        registry.add(child.relative_to(root), 'directory' if child.is_dir() else 'file')
        if child.name == 'app':
            for item in child.iterdir():
                registry.add(item.relative_to(root), 'directory' if item.is_dir() else 'file')
                if not excluded(item.relative_to(root)):
                    _relative(item.relative_to(root).as_posix())
                    entries.append(item.relative_to(root).as_posix())
        elif not excluded(child.relative_to(root)):
            _relative(child.relative_to(root).as_posix())
            entries.append(child.relative_to(root).as_posix())
    return set(entries)


def _remove(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def recover_restore(root):
    """Called only under runtime_lock; roll back an interrupted directory switch."""
    journal = root / 'app' / JOURNAL
    if not journal.exists():
        return
    if journal.is_symlink():
        raise ValueError('Invalid restore journal')
    state = json.loads(journal.read_text(encoding='utf-8'))
    recovery_name = state['recovery']
    if not recovery_name.startswith('restore-') or Path(recovery_name).name != recovery_name:
        raise ValueError('Invalid recovery directory')
    recovery = checked_root(root / 'app/recovery' / recovery_name)
    validated = []
    registry = _PathRegistry()
    seen = set()
    # Validate the entire journal before touching even one live entry.
    for entry in state['entries']:
        # Sidecars are never accepted from archives; only the old local payload
        # may contain them when replacing a corrupt database.
        relative = Path(entry['path']) if entry['path'] in DB_SIDECARS else _relative(entry['path'])
        if not (len(relative.parts) == 1 and relative.name != 'app'
                or len(relative.parts) == 2 and relative.parts[0] == 'app'):
            raise ValueError('Invalid restore journal entry')
        registry.add(relative, 'payload')
        if relative in seen or type(entry['old']) is not bool:
            raise ValueError('Invalid restore journal entry')
        seen.add(relative)
        validated.append((entry, relative))
    for entry, relative in reversed(validated):
        live, old = root / relative, recovery / relative
        if entry['old'] and old.exists():
            _remove(live)
            os.replace(old, live)
            fsync_dir(old.parent)
            fsync_dir(live.parent)
        elif not entry['old']:
            _remove(live)
            fsync_dir(live.parent)
    journal.unlink()
    fsync_dir(journal.parent)


def _switch(root, stage):
    """Keep app/ and its lock inodes stable; journal before the first rename."""
    registry = _PathRegistry()
    old_entries, new_entries = _payload(root, registry), _payload(stage, registry)
    for name in DB_SIDECARS:
        path = root / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError('Database sidecar must be a regular file')
        if path.exists():
            old_entries.add(name)
    recovery = root / 'app/recovery' / ('restore-' + uuid.uuid4().hex)
    checked_root(recovery)
    recovery.mkdir(mode=0o700, parents=True)
    entries = [{'path': name, 'old': name in old_entries} for name in sorted(old_entries | new_entries)]
    journal = root / 'app' / JOURNAL
    write_json(journal, {'recovery': recovery.name, 'entries': entries})
    try:
        for entry in entries:
            relative = Path(entry['path'])
            live, old, incoming = root / relative, recovery / relative, stage / relative
            if entry['old']:
                old.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.replace(live, old)
                fsync_dir(old.parent)
                fsync_dir(live.parent)
            if entry['path'] in new_entries:
                os.replace(incoming, live)
                fsync_dir(live.parent)
        integrity(root / DB_REL)
        journal.unlink()
        fsync_dir(journal.parent)
    except BaseException:
        recover_restore(root)
        raise
    return recovery


def _existing_integrity(root):
    try:
        integrity(root / DB_REL)
    except sqlite3.OperationalError as exc:
        # A hot rollback journal requires writes even for integrity_check.
        # Diagnose a copy so SQLite never changes the original recovery bytes.
        if getattr(exc, 'sqlite_errorcode', None) != sqlite3.SQLITE_READONLY_ROLLBACK:
            raise
        with tempfile.TemporaryDirectory(prefix='.maintenance-diagnose-', dir=root.parent) as temp:
            probe = Path(temp)
            for name in {str(DB_REL), *DB_SIDECARS}:
                source = root / name
                if source.is_symlink() or (source.exists() and not source.is_file()):
                    raise ValueError('Database and sidecars must be regular files')
                if source.exists():
                    shutil.copy2(source, probe / source.name)
            integrity(probe / DB_REL.name, readonly=False)


def restore_runtime(root, archive, *, allow_desktop_lock=False, expected_sha256=None):
    """Explicit replacement after native confirmation and backend shutdown.

    Retains a redacted archive and the exact private old payload. An interrupted
    switch is recovered on the next runtime_lock entry, before SQLite opens.
    A corrupt existing database skips only the SQLite safety archive: its raw
    database and sidecars are retained in recovery_dir. The result then includes
    safety_backup=null, previous_database_corrupt=true and a warnings list.
    """
    archive = Path(archive).expanduser().resolve(strict=True)
    if owned_by_process(root):
        raise RuntimeBusyError('Stop the backend before maintenance')
    with check_legacy_desktop(root, allow_desktop_lock=allow_desktop_lock):
        with runtime_lock(root) as root:
            from .migrations import recover_upgrade
            recover_upgrade(root / DB_REL)
            inventory(root)
            with tempfile.TemporaryDirectory(prefix='.maintenance-restore-', dir=root.parent) as temp:
                stage = Path(temp)
                with archive.open('rb') as stream:
                    digest = _archive_digest(stream)
                    if expected_sha256 is not None and digest != expected_sha256:
                        raise ValueError('Archive changed since confirmation; inspect it again')
                    manifest = _extract_verified(stream, stage)
                    if _archive_digest(stream) != digest:
                        raise ValueError('Archive changed during restore validation')
                source = manifest.get('source_root')
                if not isinstance(source, str) or not Path(source).is_absolute() or '..' in Path(source).parts:
                    raise ValueError('Invalid source root')
                relocate(stage, Path(source), root)
                sync_tree(stage)
                safety = None
                corrupt = False
                if (root / DB_REL).exists():
                    try:
                        _existing_integrity(root)
                    except DatabaseIntegrityError:
                        corrupt = True
                if (root / DB_REL).exists() and not corrupt:
                    # Drain a crashed writer's WAL before taking the safety copy.
                    # No cooperating backend can connect while we hold the lock.
                    conn = sqlite3.connect(root / DB_REL, timeout=0)
                    try:
                        if conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]:
                            raise ValueError('Database still has active readers or writers')
                        conn.execute('PRAGMA journal_mode=DELETE')
                    finally:
                        conn.close()
                    safety = root / 'app/backups' / ('before-restore-' + uuid.uuid4().hex + '.zip')
                    _backup_locked(root, safety)
                if not corrupt:
                    for name in DB_SIDECARS:
                        if (root / name).exists():
                            raise ValueError('Database sidecars remain; close all connections before restoring')
                recovery = _switch(root, stage)
            return {'ok': True, 'archive': str(archive), 'root': str(root),
                    'safety_backup': str(safety) if safety else None,
                    'recovery_dir': str(recovery), 'requires_key_reentry': True,
                    'previous_database_corrupt': corrupt,
                    'warnings': ['Previous database is corrupt; no SQLite safety archive was created. '
                                 'Original database and sidecars are retained in recovery_dir.'] if corrupt else [],
                    'secrets_included': False, 'archive_sha256': digest, 'manifest': manifest}
