"""O-OBS-1: /api/health exposes lightweight, non-fatal operational metrics."""

from __future__ import annotations

import json
import logging
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def status_db(tmp_path):
    path = tmp_path / 'metrics.db'
    connection = sqlite3.connect(path)
    connection.execute('CREATE TABLE agent_actions (id INTEGER PRIMARY KEY, status TEXT)')
    connection.execute('CREATE TABLE ingest_jobs (id INTEGER PRIMARY KEY, status TEXT)')
    connection.executemany('INSERT INTO agent_actions (status) VALUES (?)',
                           [('proposed',), ('approved',), ('approved',)])
    connection.executemany('INSERT INTO ingest_jobs (status) VALUES (?)',
                           [('queued',), ('succeeded',)])
    connection.commit()
    connection.close()
    return path


def _patch_metrics(monkeypatch, db_path):
    from app import main
    import sqlite3 as sqlite

    def connect():
        connection = sqlite.connect(db_path)
        connection.row_factory = sqlite.Row
        return connection

    monkeypatch.setattr(main.db, 'connect', connect)
    monkeypatch.setattr(main, 'table_counts', lambda: {'agent_actions': 3, 'ingest_jobs': 2})
    return main


def test_health_exposes_scan_trigger_and_queue_metrics(monkeypatch, status_db):
    main = _patch_metrics(monkeypatch, status_db)
    monkeypatch.setattr(main, '_last_scan_at', '2026-10-17T12:00:00+00:00')
    monkeypatch.setattr(main, '_trigger_stats',
                        {'source_arrived': 1, 'event_due': 0, 'workflow_waiting': 0,
                         'goal_stale': 2, 'errors': 0})
    payload = main.health()
    assert payload['status'] == 'ok'
    assert payload['tables'] == {'agent_actions': 3, 'ingest_jobs': 2}
    assert payload['last_scan_at'] == '2026-10-17T12:00:00+00:00'
    assert payload['trigger_stats']['goal_stale'] == 2
    assert payload['action_counts'] == {'proposed': 1, 'approved': 2}
    assert payload['ingest_queue'] == {'queued': 1, 'succeeded': 1}


def test_health_starts_with_empty_metrics(monkeypatch, status_db):
    main = _patch_metrics(monkeypatch, status_db)
    monkeypatch.setattr(main, '_last_scan_at', None)
    monkeypatch.setattr(main, '_trigger_stats', None)
    payload = main.health()
    assert payload['nonce'] == ''
    assert payload['last_scan_at'] is None
    assert payload['trigger_stats'] is None


def test_health_never_raises_when_metrics_store_is_broken(monkeypatch):
    from app import main

    def broken_connect():
        raise sqlite3.OperationalError('database is locked')

    monkeypatch.setattr(main.db, 'connect', broken_connect)
    monkeypatch.setattr(main, 'table_counts', lambda: {})
    payload = main.health()
    assert payload['status'] == 'ok'
    assert payload['action_counts'] is None
    assert payload['ingest_queue'] is None


def test_missing_status_table_yields_none(monkeypatch, tmp_path):
    from app import main
    empty = tmp_path / 'empty.db'
    sqlite3.connect(empty).close()

    def connect():
        return sqlite3.connect(empty)

    monkeypatch.setattr(main.db, 'connect', connect)
    assert main._status_counts('agent_actions') is None


def test_scan_loop_records_scan_time_and_trigger_stats(monkeypatch):
    from app import main
    monkeypatch.setattr(main, '_last_scan_at', None)
    monkeypatch.setattr(main, '_trigger_stats', None)
    main._stop_scan.clear()

    def scan_all_sources():
        return {'course-a': {'new': 1}}

    def run_agent_triggers():
        main._stop_scan.set()
        return {'source_arrived': 1, 'errors': 0}

    monkeypatch.setattr(main, 'scan_all_sources', scan_all_sources)
    monkeypatch.setattr(main, 'run_agent_triggers', run_agent_triggers)
    main._scan_loop()
    assert main._last_scan_at is not None
    assert main._trigger_stats == {'source_arrived': 1, 'errors': 0}


def test_scan_loop_logs_failures_without_dying(monkeypatch, caplog):
    from app import main
    monkeypatch.setattr(main, '_last_scan_at', None)
    main._stop_scan.clear()

    def broken_scan():
        raise RuntimeError('scan exploded')

    def stop_triggers():
        main._stop_scan.set()
        return {'errors': 1}

    monkeypatch.setattr(main, 'scan_all_sources', broken_scan)
    monkeypatch.setattr(main, 'run_agent_triggers', stop_triggers)
    with caplog.at_level(logging.ERROR):
        main._scan_loop()
    assert main._last_scan_at is None
    assert 'background source scan failed' in caplog.text
    assert 'scan exploded' in caplog.text


def test_health_endpoint_serves_the_metrics(monkeypatch, status_db):
    main = _patch_metrics(monkeypatch, status_db)
    response = TestClient(main.app).get('/api/health')
    assert response.status_code == 200
    payload = response.json()
    for key in ('status', 'stage', 'tables', 'nonce', 'last_scan_at',
                'trigger_stats', 'action_counts', 'ingest_queue',
                'schema_version', 'fts5_available', 'db_size_bytes',
                'last_backup_at', 'workspace_counts', 'log_file_present'):
        assert key in payload
    assert payload['action_counts'] == {'proposed': 1, 'approved': 2}
    assert payload['ingest_queue'] == {'queued': 1, 'succeeded': 1}


@pytest.fixture
def health_world(tmp_path, monkeypatch):
    from app import main

    root = tmp_path / 'data'
    db_path = root / 'app' / 'banxue.db'
    monkeypatch.setattr(main.db, 'DATA_DIR', root)
    monkeypatch.setattr(main.db, 'APP_DIR', root / 'app')
    monkeypatch.setattr(main.db, 'DB_PATH', db_path)
    main.db.apply_schema()
    connection = main.db.connect()
    try:
        studypal = connection.execute(
            "SELECT id FROM workspaces WHERE key='studypal'").fetchone()[0]
        course_id = connection.execute(
            "INSERT INTO courses(name,workspace_id) VALUES('指标课程',?)",
            (studypal,)).lastrowid
        connection.execute("INSERT INTO kp(course_id,name) VALUES(?,?)",
                           (course_id, '指标节点'))
        connection.commit()
    finally:
        connection.close()
    return main, root, db_path


def test_health_extended_schema_size_and_workspace_metrics(health_world):
    main, root, db_path = health_world
    payload = main.health()

    schema = payload['schema_version']
    assert isinstance(schema, dict)
    assert isinstance(schema['user_version'], int)
    assert isinstance(schema['migrations'], int)
    assert schema['migrations'] > 0
    connection = sqlite3.connect(str(db_path))
    try:
        expected_user_version = connection.execute('PRAGMA user_version').fetchone()[0]
        expected_migrations = connection.execute(
            'SELECT COUNT(*) FROM schema_migrations').fetchone()[0]
    finally:
        connection.close()
    assert schema == {'user_version': expected_user_version,
                      'migrations': expected_migrations}

    assert isinstance(payload['fts5_available'], bool)
    assert payload['db_size_bytes'] == db_path.stat().st_size
    assert payload['last_backup_at'] is None

    counts = payload['workspace_counts']
    assert counts['count'] == len(counts['items'])
    assert counts['count'] >= 3
    studypal = next(item for item in counts['items'] if item['workspace'] == 'studypal')
    assert studypal['kp'] == 1
    assert studypal['documents'] is None or isinstance(studypal['documents'], int)

    assert isinstance(payload['log_file_present'], bool)
    # No absolute path may leak through the health payload.
    assert str(root) not in json.dumps(payload)


def test_health_reports_only_the_timestamp_of_the_newest_backup(health_world):
    main, root, db_path = health_world
    backups = main.db.APP_DIR / 'backups'
    backups.mkdir(parents=True, exist_ok=True)
    backup = backups / 'before-upgrade-abc123.db'
    backup.write_bytes(b'fake backup payload')
    os.utime(backup, (1_700_000_000, 1_700_000_000))

    payload = main.health()
    assert payload['last_backup_at'] == '2023-11-14T22:13:20+00:00'
    assert 'before-upgrade-abc123.db' not in json.dumps(payload)
    assert str(root) not in json.dumps(payload)


def test_health_degrades_without_fts5_and_restores_none_backup(health_world, monkeypatch):
    class FailingSqlite:
        class Error(Exception):
            pass

        def connect(self, *args, **kwargs):
            raise RuntimeError('dummy sqlite without FTS5')

    main, root, db_path = health_world
    monkeypatch.setattr(main, 'sqlite3', FailingSqlite())
    payload = main.health()
    assert payload['status'] == 'ok'
    assert payload['fts5_available'] is False
    assert payload['last_backup_at'] is None


def test_health_schema_and_workspace_metrics_degrade_to_none(health_world, monkeypatch):
    main, root, db_path = health_world

    def broken_connect():
        raise sqlite3.OperationalError('database is locked')

    monkeypatch.setattr(main.db, 'connect', broken_connect)
    monkeypatch.setattr(main, 'table_counts', lambda: {})
    payload = main.health()
    assert payload['status'] == 'ok'
    assert payload['schema_version'] is None
    assert payload['workspace_counts'] is None
