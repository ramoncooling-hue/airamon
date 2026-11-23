"""Shared snapshot, path relocation and durable file primitives."""

import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
import tempfile

DB_REL = Path('app/banxue.db')
TRANSIENT = {'.DS_Store', '__pycache__'}
APP_TRANSIENT_DIRS = {Path('app/logs'), Path('app/backups'), Path('app/recovery'), Path('app/desktop'),
                      Path('app/extensions'), Path('app/vector-index'), Path('app/diagnostics'),
                      # T3.11: user-generated Markdown vaults are personal data and
                      # the whole export is regenerable, so backup/restore skips it.
                      Path('exports')}
APP_TRANSIENT_FILES = {
    Path('app/desktop-session.json'), Path('app/desktop.lock'), Path('app/runtime.lock'),
    Path('app/.restore-journal.json'), Path('app/.schema-upgrade.json'),
}


def excluded(relative):
    relative = Path(relative)
    if relative in APP_TRANSIENT_FILES or any(relative.is_relative_to(folder) for folder in APP_TRANSIENT_DIRS):
        return True
    if relative.name in TRANSIENT or relative.name.endswith(('.lock', '-wal', '-shm', '-journal')) and relative.parent == Path('app'):
        return True
    return relative.parent == Path('app') and relative.name.startswith(('.env', '.maintenance-'))


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fsync_dir(path):
    if os.name == 'nt':
        return  # Windows does not expose directory fsync through os.open.
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, value):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix='.maintenance-json-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


class DatabaseIntegrityError(ValueError):
    """Only verified SQLite corruption, never a lock or filesystem failure."""


def integrity(path, *, readonly=True):
    conn = sqlite3.connect(Path(path).as_uri() + ('?mode=ro' if readonly else '?mode=rw'), uri=True, timeout=0)
    try:
        if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise DatabaseIntegrityError('Database integrity check failed')
    except sqlite3.DatabaseError as exc:
        if getattr(exc, 'sqlite_errorcode', 0) & 0xff in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
            raise DatabaseIntegrityError('Database integrity check failed') from exc
        raise
    finally:
        conn.close()


def sqlite_snapshot(source, destination):
    integrity(source)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    src = sqlite3.connect(Path(source).as_uri() + '?mode=ro', uri=True)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
        dst.execute('PRAGMA journal_mode=DELETE')
    finally:
        dst.close()
        src.close()
    destination.chmod(0o600)
    integrity(destination)


def inventory(root):
    """Reject links and device files, rather than copying outside the runtime."""
    paths = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in list(dirs) + files:
            path = parent / name
            relative = path.relative_to(root)
            if excluded(relative):
                if name in dirs:
                    dirs.remove(name)
                continue
            mode = path.lstat().st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ValueError(f'Unsupported runtime entry: {relative}')
            if stat.S_ISREG(mode):
                paths.append(relative)
    return sorted(paths)


def require_space(parent, size):
    if shutil.disk_usage(parent).free < size + 16 * 1024 * 1024:
        raise OSError(errno.ENOSPC, 'Insufficient space for a verified snapshot')


def snapshot_tree(source, stage):
    files = inventory(source)
    if DB_REL not in files:
        raise ValueError('Runtime database is missing: app/banxue.db')
    require_space(stage.parent, sum((source / p).stat().st_size for p in files) * 2)
    for directory, dirs, _ in os.walk(source):
        parent = Path(directory)
        dirs[:] = [name for name in dirs if not excluded((parent / name).relative_to(source))]
        (stage / parent.relative_to(source)).mkdir(mode=0o700, parents=True, exist_ok=True)
    for relative in files:
        original, destination = source / relative, stage / relative
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if relative == DB_REL:
            sqlite_snapshot(original, destination)
        else:
            before = sha256(original)
            shutil.copy2(original, destination)
            destination.chmod(0o600)
            if before != sha256(destination) or before != sha256(original):
                raise ValueError(f'Runtime changed during snapshot: {relative}')
    if inventory(source) != files:
        raise ValueError('Runtime changed during snapshot')


def _root_pattern(source, *, anchored=False):
    root = str(source).rstrip('/\\')
    windows = '\\' in root or (len(root) >= 2 and root[1] == ':')
    prefix = ''.join(r'[\\/]' if char in '/\\' else re.escape(char) for char in root)
    return re.compile(('^' if anchored else r'(?<![\w\\/:])') + prefix + r'(?=$|[\\/])',
                      re.IGNORECASE if windows else 0)


def _replace_root_references(value, source, target):
    return _root_pattern(source).sub(lambda match: str(target), value)


def remap(value, source, target, *, verify=False):
    if isinstance(value, dict):
        return {k: remap(v, source, target, verify=verify) for k, v in value.items()}
    if isinstance(value, list):
        return [remap(v, source, target, verify=verify) for v in value]
    if isinstance(value, str) and _root_pattern(source, anchored=True).match(value):
        path = Path(value)
        if '..' in re.split(r'[\\/]', value):
            raise ValueError('Internal material path contains traversal')
        if verify and not path.exists():
            raise ValueError(f'Referenced runtime file is missing: {path.relative_to(source)}')
        return _root_pattern(source, anchored=True).sub(lambda match: str(target), value, count=1)
    return value


def relocate(stage, source, target, *, verify=False):
    """Relocate managed references; never rewrite original input materials."""
    for path in stage.rglob('*.json'):
        if {'inbox', 'source', 'raw'} & set(path.relative_to(stage).parts):
            continue
        try:
            original = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, UnicodeError):
            continue
        updated = remap(original, source, target, verify=verify)
        if updated != original:
            path.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding='utf-8')
    for path in stage.rglob('*.md'):
        parts = set(path.relative_to(stage).parts)
        if parts & {'notes', 'wiki'} and not parts & {'inbox', 'source', 'raw'}:
            original = path.read_text(encoding='utf-8')
            updated = _replace_root_references(original, source, target)
            if updated != original:
                path.write_text(updated, encoding='utf-8')
    conn = sqlite3.connect(stage / DB_REL)
    try:
        # table_list distinguishes FTS shadow tables from ordinary application tables.
        tables = [r[1] for r in conn.execute('PRAGMA table_list')
                  if r[2] == 'table' and not r[1].startswith('sqlite_')]
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            for column in conn.execute(f'PRAGMA table_info({quoted})').fetchall():
                name = column[1]
                if name not in {'path', 'source'} and not name.endswith(('_path', '_json')):
                    continue
                field = '"' + name.replace('"', '""') + '"'
                for rowid, original in conn.execute(f'SELECT rowid,{field} FROM {quoted}').fetchall():
                    if not isinstance(original, str) or not original:
                        continue
                    if name.endswith('_json'):
                        try:
                            data = json.loads(original)
                        except ValueError:
                            continue
                        updated = json.dumps(remap(data, source, target, verify=verify), ensure_ascii=False)
                    elif name == 'source':
                        updated = _replace_root_references(original, source, target)
                    else:
                        updated = remap(original, source, target, verify=verify)
                    if updated != original:
                        conn.execute(f'UPDATE {quoted} SET {field}=? WHERE rowid=?', (updated, rowid))
        conn.commit()
    finally:
        conn.close()
    integrity(stage / DB_REL)


def sync_tree(root):
    for directory, _, files in os.walk(root, topdown=False):
        for name in files:
            with (Path(directory) / name).open('rb') as stream:
                os.fsync(stream.fileno())
        fsync_dir(directory)


def rename_new(source, target):
    """Atomic publication with no overwrite, including an empty competing dir."""
    if sys.platform == 'win32':
        os.rename(source, target)
        fsync_dir(Path(target).parent)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin':
        result = libc.renamex_np(os.fsencode(source), os.fsencode(target), 0x4)  # RENAME_EXCL
    elif sys.platform.startswith('linux'):
        result = libc.renameat2(-100, os.fsencode(source), -100, os.fsencode(target), 1)
    else:
        raise OSError('Exclusive directory rename is unsupported on this platform')
    if result:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), str(target))
    fsync_dir(Path(target).parent)
