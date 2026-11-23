import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import zipfile
from unittest.mock import patch

import pytest

from app.maintenance import backup_runtime, restore_runtime, runtime_lock, RuntimeBusyError
from app.maintenance import backup, files, migrations
from app.memory import db, seed


def initialize(root):
    root = root.resolve()
    with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / files.DB_REL):
        db.apply_schema()
    material = root / 'StudyPal/course/inbox/material.md'
    material.parent.mkdir(parents=True)
    material.write_text('original material', encoding='utf-8')
    (root / 'StudyPal/empty').mkdir()
    (root / 'app/settings.json').write_text(json.dumps({
        'fast': {'model': 'local', 'api_key': 'SECRET-TEST'},
        'channels': {'tsinghua_password': 'SECRET-TEST'},
        'nested': [{'access_token': 'SECRET-TEST'}],
        'local_sources': {'course': [str(material.parent), '/outside/folder']},
    }))
    conn = sqlite3.connect(root / files.DB_REL)
    try:
        conn.execute("INSERT INTO courses(name) VALUES('fixture')")
        cid = conn.execute('SELECT id FROM courses').fetchone()[0]
        conn.execute("INSERT INTO kp(course_id,name,source) VALUES(?, 'first', ?)", (cid, str(material)))
        conn.execute("INSERT INTO kp(course_id,name) VALUES(?, 'second')", (cid,))
        conn.execute("INSERT INTO kp_edges(from_kp,to_kp) VALUES(1,2)")
        conn.execute("INSERT INTO events(title) VALUES('date')")
        conn.execute("INSERT INTO conversations(title) VALUES('chat')")
        conn.execute("INSERT INTO workflows(key,name) VALUES('flow','flow')")
        conn.commit()
    finally:
        conn.close()
    hub = root / 'app/hub/agents'
    hub.mkdir(parents=True)
    (hub / 'agent.md').write_text('agent')
    return root


def read_rows(path, sql):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_roundtrip_complete_business_data_and_redacted_settings(tmp_path):
    source = initialize(tmp_path / 'source')
    archive = tmp_path / 'backup.zip'
    result = backup_runtime(source, archive)
    assert result['requires_key_reentry']
    with zipfile.ZipFile(archive) as z:
        assert b'SECRET-TEST' not in z.read('data/app/settings.json')
        assert all(not any(x in name for x in ('runtime.lock', 'backups/', 'logs/')) for name in z.namelist())
    target = tmp_path / 'target'
    restored = restore_runtime(target, archive)
    assert restored['ok']
    for table in ('courses', 'kp', 'kp_edges', 'events', 'conversations', 'workflows'):
        assert read_rows(source / files.DB_REL, f'SELECT count(*) FROM {table}') == read_rows(target / files.DB_REL, f'SELECT count(*) FROM {table}')
    reference = read_rows(target / files.DB_REL, 'SELECT source FROM kp WHERE id=1')[0][0]
    assert Path(reference).read_text() == 'original material'
    assert (target / 'StudyPal/empty').is_dir()
    assert (target / 'app/hub/agents/agent.md').read_text() == 'agent'
    settings = json.loads((target / 'app/settings.json').read_text())
    assert settings['fast'] == {'model': 'local', 'api_key': ''}
    assert settings['local_sources']['course'] == [str(target / 'StudyPal/course/inbox'), '/outside/folder']
    assert 'SECRET-TEST' in (source / 'app/settings.json').read_text()


def test_restore_preserves_lock_inode_and_keeps_original(tmp_path):
    source, target = initialize(tmp_path / 'source'), initialize(tmp_path / 'target')
    archive = tmp_path / 'backup.zip'
    backup_runtime(source, archive)
    lock = target / 'app/desktop.lock'
    lock.touch()
    inodes = [(target / 'app').stat().st_ino, lock.stat().st_ino, (target / 'app/runtime.lock').stat().st_ino]
    result = restore_runtime(target, archive)
    assert inodes == [(target / 'app').stat().st_ino, lock.stat().st_ino, (target / 'app/runtime.lock').stat().st_ino]
    assert Path(result['safety_backup']).is_file()
    assert 'SECRET-TEST' in (Path(result['recovery_dir']) / 'app/settings.json').read_text()


def test_extension_trust_and_cache_are_not_exported_or_replaced(tmp_path):
    source, target = initialize(tmp_path / 'source'), initialize(tmp_path / 'target')
    for root, value in [(source, 'source-secret'), (target, 'local-trust')]:
        for folder, filename in [('extensions', 'mcp.json'), ('vector-index', 'fixture.db')]:
            directory = root / 'app' / folder
            directory.mkdir()
            (directory / filename).write_text(value)
    archive = tmp_path / 'backup.zip'
    backup_runtime(source, archive)
    with zipfile.ZipFile(archive) as z:
        assert not any('/extensions/' in name or '/vector-index/' in name for name in z.namelist())
    restore_runtime(target, archive)
    assert (target / 'app/extensions/mcp.json').read_text() == 'local-trust'
    assert (target / 'app/vector-index/fixture.db').read_text() == 'local-trust'


@pytest.mark.parametrize('bad_name', ['data/../escape', '/absolute', 'data/app/runtime.lock', 'data/a\\b', 'data/a/../../bad'])
def test_archive_paths_rejected_without_touching_existing(tmp_path, bad_name):
    target = initialize(tmp_path / 'target')
    before = (target / files.DB_REL).read_bytes()
    archive = tmp_path / 'bad.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr(bad_name, 'bad')
    with pytest.raises(ValueError):
        restore_runtime(target, archive)
    assert (target / files.DB_REL).read_bytes() == before
    assert not (tmp_path / 'escape').exists()


@pytest.mark.parametrize('kind', ['hash', 'symlink', 'oversize', 'duplicate', 'missing'])
def test_corrupt_archive_rejected(tmp_path, kind):
    root = initialize(tmp_path / 'source')
    good, bad = tmp_path / 'good.zip', tmp_path / 'bad.zip'
    backup_runtime(root, good)
    with zipfile.ZipFile(good) as original, zipfile.ZipFile(bad, 'w') as out:
        for info in original.infolist():
            content = original.read(info)
            if info.filename == 'data/app/settings.json':
                if kind == 'hash':
                    content = b'x' * len(content)
                if kind == 'symlink':
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                if kind == 'missing':
                    continue
            out.writestr(info, content)
        if kind == 'duplicate':
            with pytest.warns(UserWarning):
                out.writestr('manifest.json', '{}')
    with patch.object(backup, 'MAX_EXPANDED_BYTES', 1 if kind == 'oversize' else backup.MAX_EXPANDED_BYTES):
        with pytest.raises(ValueError):
            restore_runtime(tmp_path / 'target', bad)
    assert not (tmp_path / 'target' / files.DB_REL).exists()


def test_runtime_lock_blocks_other_process_and_thread(tmp_path):
    root = initialize(tmp_path / 'source')
    with runtime_lock(root):
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(backup_runtime, root, tmp_path / 'blocked.zip')
            with pytest.raises(RuntimeBusyError):
                future.result()
        proc = subprocess.run([sys.executable, 'scripts/backup_runtime.py', '--data-dir', str(root),
                               'backup', '--output', str(tmp_path / 'other.zip')], capture_output=True, text=True)
        assert proc.returncode == 1
        assert 'Runtime is in use' in proc.stderr


def test_native_maintenance_legacy_lock_and_owner_pid(tmp_path):
    from app.maintenance.lock import _acquire, _release
    root = initialize(tmp_path / 'source')
    with (root / 'app/desktop.lock').open('a+b') as lock:
        _acquire(lock.fileno())
        with pytest.raises(RuntimeBusyError):
            backup_runtime(root, tmp_path / 'blocked.zip')
        with patch.dict(os.environ, {'MINDOCK_MAINTENANCE_OWNER_PID': 'invalid'}):
            with pytest.raises(RuntimeBusyError):
                backup_runtime(root, tmp_path / 'badowner.zip', allow_desktop_lock=True)
        with patch.dict(os.environ, {'MINDOCK_MAINTENANCE_OWNER_PID': str(os.getppid())}):
            assert backup_runtime(root, tmp_path / 'native.zip', allow_desktop_lock=True)['ok']
        _release(lock.fileno())


def test_json_writer_recovers_from_old_tmp(tmp_path):
    target = tmp_path / 'journal.json'
    target.with_name('journal.json.tmp').write_text('interrupted')
    files.write_json(target, {'ok': True})
    assert json.loads(target.read_text()) == {'ok': True}
    assert not list(tmp_path.glob('.maintenance-json-*'))


def test_restore_failure_rolls_back_all_payload(tmp_path):
    source, target = initialize(tmp_path / 'source'), initialize(tmp_path / 'target')
    archive = tmp_path / 'backup.zip'
    backup_runtime(source, archive)
    before_db = (target / files.DB_REL).read_bytes()
    before_settings = (target / 'app/settings.json').read_bytes()
    real_replace = os.replace
    def fail_incoming(src, dst):
        if '.maintenance-restore-' in str(src) and str(dst).endswith('settings.json'):
            raise PermissionError('injected replacement failure')
        return real_replace(src, dst)
    with patch.object(backup.os, 'replace', side_effect=fail_incoming):
        with pytest.raises(PermissionError):
            restore_runtime(target, archive)
    assert (target / 'app/settings.json').read_bytes() == before_settings
    assert read_rows(target / files.DB_REL, 'SELECT name FROM courses') == [('fixture',)]
    assert not (target / 'app/.restore-journal.json').exists()


def test_interrupted_restore_recovered_before_lock_yields(tmp_path):
    root = initialize(tmp_path / 'source')
    recovery = root / 'app/recovery/restore-test/app'
    recovery.mkdir(parents=True)
    original = (root / 'app/settings.json').read_bytes()
    os.replace(root / 'app/settings.json', recovery / 'settings.json')
    (root / 'app/settings.json').write_text('{}')
    files.write_json(root / 'app/.restore-journal.json', {
        'recovery': 'restore-test', 'entries': [{'path': 'app/settings.json', 'old': True}]})
    with runtime_lock(root):
        assert (root / 'app/settings.json').read_bytes() == original


def test_upgrade_is_once_only_and_backfills_new_seed_rows(tmp_path):
    root = initialize(tmp_path / 'source')
    database = root / files.DB_REL
    before = read_rows(database, 'SELECT * FROM schema_migrations')
    with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', database):
        db.apply_schema()
        assert read_rows(database, 'SELECT workspace_id FROM courses')[0][0] is not None
        db.apply_schema()
    assert before == read_rows(database, 'SELECT * FROM schema_migrations')
    assert not list((root / 'app/backups').glob('before-upgrade-*'))


def test_old_database_upgrade_and_second_statement_failure(tmp_path):
    root = tmp_path / 'root'
    (root / 'app').mkdir(parents=True)
    database = root / files.DB_REL
    conn = sqlite3.connect(database)
    conn.execute('CREATE TABLE personal(value TEXT)')
    conn.execute("INSERT INTO personal VALUES('keep')")
    conn.commit()
    conn.close()
    schemas = tmp_path / 'schema'
    schemas.mkdir()
    (schemas / '001_test.sql').write_text('CREATE TABLE new_table(id INTEGER); INVALID SQL;')
    with pytest.raises(migrations.SchemaUpgradeError) as error:
        migrations.upgrade_database(database, schemas, legacy_upgrade=lambda c: None, seed_backfill=lambda c: None)
    assert Path(error.value.backup_path).is_file()
    assert read_rows(database, 'SELECT * FROM personal') == [('keep',)]
    assert not read_rows(database, "SELECT name FROM sqlite_master WHERE name='new_table'")
    (schemas / '001_test.sql').write_text('CREATE TABLE new_table(id INTEGER);')
    result = migrations.upgrade_database(database, schemas, legacy_upgrade=lambda c: None, seed_backfill=lambda c: None)
    assert result['applied']
    assert read_rows(database, 'SELECT * FROM personal') == [('keep',)]


def test_interrupted_schema_recovery_and_older_app_guard(tmp_path):
    root = initialize(tmp_path / 'root')
    database = root / files.DB_REL
    directory = root / 'app/backups'
    directory.mkdir()
    snapshot = directory / 'before-upgrade-test.db'
    files.sqlite_snapshot(database, snapshot)
    files.write_json(root / 'app/.schema-upgrade.json', {
        'database': database.name, 'backup': snapshot.name, 'sha256': files.sha256(snapshot)})
    conn = sqlite3.connect(database)
    conn.execute('CREATE TABLE partial(id INTEGER)')
    conn.commit()
    conn.close()
    with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', database):
        with runtime_lock(root):
            db.apply_schema()
        assert not read_rows(database, "SELECT name FROM sqlite_master WHERE name='partial'")
        conn = sqlite3.connect(database)
        conn.execute('PRAGMA user_version=999')
        conn.commit()
        conn.close()
        before = database.read_bytes()
        with pytest.raises(migrations.SchemaCompatibilityError):
            db.connect()
        assert database.read_bytes() == before


def test_checksum_drift_rejected(tmp_path):
    root = initialize(tmp_path / 'root')
    schema = tmp_path / 'schema'
    shutil.copytree(db.SCHEMA_DIR, schema)
    with (schema / '001_memory.sql').open('a') as stream:
        stream.write('\n-- tampered\n')
    with pytest.raises(migrations.SchemaCompatibilityError, match='checksum'):
        migrations.assert_compatible(root / files.DB_REL, schema)


def test_seed_refresh_worker_under_main_lifetime_lock(tmp_path):
    root = initialize(tmp_path / 'root')
    public = tmp_path / 'seed'
    public.mkdir()
    (public / 'dash_courses.json').write_text('[{"课程":"new course"}]')
    with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / files.DB_REL), \
            patch.object(seed, 'APP_DIR', root / 'app'), patch.object(seed, 'SEED_DIR', public):
        with runtime_lock(root), concurrent.futures.ThreadPoolExecutor() as pool:
            assert pool.submit(seed.refresh).result(timeout=15)['ok']
        assert read_rows(root / files.DB_REL, "SELECT workspace_id IS NOT NULL FROM courses WHERE name='new course'") == [(1,)]


def test_source_startup_and_cli_roundtrip(tmp_path):
    root = tmp_path / 'root'
    env = {**os.environ, 'CYBERPAL_DATA_DIR': str(root)}
    code = ('from fastapi.testclient import TestClient; from app.main import app; '
            '\nwith TestClient(app) as client:\n assert client.get("/api/health").status_code == 200\n')
    proc = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    archive = tmp_path / 'cli.zip'
    for arguments in [ ['--data-dir', str(root), 'backup', '--output', str(archive)],
                       ['--data-dir', str(tmp_path / 'restored'), 'restore', '--input', str(archive)]]:
        proc = subprocess.run([sys.executable, 'scripts/backup_runtime.py', *arguments],
                              env=env, capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout)['ok']


def test_nested_logs_directory_is_preserved(tmp_path):
    root = tmp_path / 'runtime'
    (root / 'app').mkdir(parents=True)
    nested = root / 'StudyPal' / '会计学' / 'inbox' / 'logs'
    nested.mkdir(parents=True)
    (nested / 'notes.txt').write_text('课程材料', encoding='utf-8')
    # The test only needs a valid SQLite file for the snapshot contract.
    sqlite3.connect(root / files.DB_REL).close()
    stage = tmp_path / 'stage'
    files.snapshot_tree(root, stage)
    assert (stage / 'StudyPal' / '会计学' / 'inbox' / 'logs' / 'notes.txt').read_text(encoding='utf-8') == '课程材料'


def test_inspect_during_runtime_lock_and_confirmation_hash(tmp_path):
    from scripts.backup_runtime import run_maintenance
    source = initialize(tmp_path / 'source')
    archive = tmp_path / 'archive.zip'
    backup_runtime(source, archive)
    with runtime_lock(source):
        summary = run_maintenance('inspect', source, archive)
    assert summary['courses'] == 1
    assert summary['kp'] == 2
    assert summary['events'] == 1
    assert summary['sha256'] == summary['archive_sha256'] == files.sha256(archive)
    assert summary['secrets_included'] is False
    target = initialize(tmp_path / 'target')
    before = (target / files.DB_REL).read_bytes()
    with pytest.raises(ValueError, match='confirmation'):
        restore_runtime(target, archive, expected_sha256='0' * 64)
    assert (target / files.DB_REL).read_bytes() == before
    assert restore_runtime(target, archive, expected_sha256=summary['sha256'])['ok']


def test_crashed_upgrade_process_recovers_and_retries(tmp_path):
    root = initialize(tmp_path / 'root')
    schema = tmp_path / 'schema'
    shutil.copytree(db.SCHEMA_DIR, schema)
    next_number = max(int(p.name.split('_')[0].rstrip('b')) for p in schema.glob('*.sql')) + 1
    (schema / f'{next_number:03d}_crash.sql').write_text('CREATE TABLE upgraded(id INTEGER);')
    code = '''
import os, sys
from pathlib import Path
from app.maintenance import migrations
original = migrations.execute_sql
def interrupt(conn, sql, *args, **kwargs):
    original(conn, sql)
    os._exit(77)
migrations.execute_sql = interrupt
migrations.upgrade_database(Path(sys.argv[1]), Path(sys.argv[2]))
'''
    proc = subprocess.run([sys.executable, '-c', code, str(root / files.DB_REL), str(schema)], timeout=15)
    assert proc.returncode == 77
    assert (root / 'app/.schema-upgrade.json').exists()
    migrations.upgrade_database(root / files.DB_REL, schema)
    assert read_rows(root / files.DB_REL, "SELECT name FROM sqlite_master WHERE name='upgraded'") == [('upgraded',)]
    assert read_rows(root / files.DB_REL, 'SELECT name FROM courses') == [('fixture',)]
    assert not (root / 'app/.schema-upgrade.json').exists()


def test_crashed_restore_process_rolls_back_on_next_start(tmp_path):
    source, target = initialize(tmp_path / 'source'), initialize(tmp_path / 'target')
    (target / 'StudyPal/course/inbox/material.md').write_text('target original')
    archive = tmp_path / 'backup.zip'
    backup_runtime(source, archive)
    code = '''
import os, sys
from pathlib import Path
from app.maintenance import backup
original = os.replace
def interrupt(source, destination):
    original(source, destination)
    if '.maintenance-restore-' in str(source):
        os._exit(78)
backup.os.replace = interrupt
backup.restore_runtime(Path(sys.argv[1]), Path(sys.argv[2]))
'''
    proc = subprocess.run([sys.executable, '-c', code, str(target), str(archive)], timeout=15)
    assert proc.returncode == 78
    with runtime_lock(target):
        assert (target / 'StudyPal/course/inbox/material.md').read_text() == 'target original'
        assert 'SECRET-TEST' in (target / 'app/settings.json').read_text()
    assert not (target / 'app/.restore-journal.json').exists()


def test_restore_over_corrupt_database_preserves_raw_original_and_sidecars(tmp_path):
    source, target = initialize(tmp_path / 'source'), initialize(tmp_path / 'target')
    archive = tmp_path / 'backup.zip'
    backup_runtime(source, archive)
    database = target / files.DB_REL
    database.write_bytes(b'corrupted original database' * 200)
    originals = {database: database.read_bytes()}
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = Path(str(database) + suffix)
        sidecar.write_bytes(b'original sidecar ' + suffix.encode())
        originals[sidecar] = sidecar.read_bytes()
    result = restore_runtime(target, archive)
    assert result['ok'] and result['previous_database_corrupt']
    assert result['safety_backup'] is None
    assert result['warnings']
    recovery = Path(result['recovery_dir'])
    for path, content in originals.items():
        assert (recovery / path.relative_to(target)).read_bytes() == content
        if path != database:
            assert not path.exists()
    files.integrity(database)
    assert read_rows(database, 'SELECT name FROM courses') == [('fixture',)]


def test_restore_does_not_treat_busy_database_as_corrupt(tmp_path):
    source, target = initialize(tmp_path / 'source'), initialize(tmp_path / 'target')
    archive = tmp_path / 'backup.zip'
    backup_runtime(source, archive)
    database = target / files.DB_REL
    conn = sqlite3.connect(database)
    try:
        conn.execute('PRAGMA journal_mode=DELETE')
        conn.execute('BEGIN EXCLUSIVE')
        before = database.read_bytes()
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            restore_runtime(target, archive)
        assert database.read_bytes() == before
        assert not (target / 'app/recovery').exists()
    finally:
        conn.rollback()
        conn.close()


def archive_with_paths(good, output, additions, directories=()):
    import hashlib
    with zipfile.ZipFile(good) as original, zipfile.ZipFile(output, 'w') as archive:
        manifest = json.loads(original.read('manifest.json'))
        extra = [{'path': name, 'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
                 for name, content in additions]
        manifest['files'] = extra + manifest['files']
        manifest['directories'].extend(directories)
        archive.writestr('manifest.json', json.dumps(manifest))
        for name, content in additions:
            archive.writestr('data/' + name, content)
        for info in original.infolist():
            if info.filename != 'manifest.json':
                archive.writestr(info, original.read(info))


@pytest.mark.parametrize(('paths', 'directories'), [
    ([('APP/settings.json', b'{}')], []),
    ([('Folder/a.txt', b'a'), ('folder/b.txt', b'b')], []),
    ([('Caf\u00e9/a.txt', b'a'), ('Cafe\u0301/b.txt', b'b')], []),
    ([('Folder', b'a'), ('folder/b.txt', b'b')], []),
    ([('Folder', b'a')], ['folder']),
    ([], ['APP']),
    ([], ['StudyPal/COURSE']),
    ([('app/Settings.json', b'{}')], []),
    ([('app/Runtime.lock', b'lock')], []),
])
def test_archive_component_aliases_rejected_before_extraction(tmp_path, paths, directories):
    source = initialize(tmp_path / 'source')
    good, bad = tmp_path / 'good.zip', tmp_path / 'bad.zip'
    backup_runtime(source, good)
    archive_with_paths(good, bad, paths, directories)
    stage = tmp_path / 'stage'
    stage.mkdir()
    with pytest.raises(ValueError):
        backup._extract_verified(bad, stage)
    assert list(stage.iterdir()) == []
    with pytest.raises(ValueError):
        backup.inspect_archive(bad)


def test_app_alias_repro_on_case_insensitive_filesystem(tmp_path):
    source, target = initialize(tmp_path / 'source'), initialize(tmp_path / 'target')
    if not (target / 'APP').exists():
        pytest.skip('Requires case-insensitive filesystem')
    assert os.path.samefile(target / 'app', target / 'APP')
    good, bad = tmp_path / 'good.zip', tmp_path / 'bad.zip'
    backup_runtime(source, good)
    archive_with_paths(good, bad, [('APP/settings.json', b'{}')])
    original = (target / files.DB_REL).read_bytes()
    inode = (target / 'app/runtime.lock').stat().st_ino
    with pytest.raises(ValueError):
        restore_runtime(target, bad)
    assert (target / files.DB_REL).read_bytes() == original
    assert (target / 'app/runtime.lock').stat().st_ino == inode
    assert not (target / 'app/recovery').exists()


def test_payload_and_rollback_reject_app_alias_before_mutation(tmp_path):
    root = initialize(tmp_path / 'root')
    alias_root = tmp_path / 'alias-root'
    alias_root.mkdir()
    (alias_root / 'APP').mkdir()
    with pytest.raises(ValueError):
        backup._payload(alias_root)
    original = (root / files.DB_REL).read_bytes()
    sentinel = root / 'keep.txt'
    sentinel.write_text('keep')
    # Reversal would remove keep.txt first if entries were validated lazily.
    files.write_json(root / 'app/.restore-journal.json', {
        'recovery': 'restore-test', 'entries': [
            {'path': 'APP', 'old': False}, {'path': 'keep.txt', 'old': False}]})
    with pytest.raises(ValueError):
        backup.recover_restore(root)
    assert (root / files.DB_REL).read_bytes() == original
    assert sentinel.read_text() == 'keep'


def test_switch_rejects_alias_between_existing_and_incoming(tmp_path):
    root, stage = initialize(tmp_path / 'root'), initialize(tmp_path / 'stage')
    (root / 'Custom').mkdir()
    (stage / 'custom').mkdir()
    with pytest.raises(ValueError):
        backup._switch(root, stage)
    assert not (root / 'app/recovery').exists()
