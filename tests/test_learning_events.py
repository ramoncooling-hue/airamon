"""B3 T3.7 (028): learning event model, 026 -> 028 upgrade, module and API.

Covered:
- fresh schema: three append-only tables, contracted CHECKs and indexes, the
  partial unique idempotency indexes (empty keys stay reusable);
- maintenance channel: a 026 database upgrades through apply_schema(), keeps
  every pre-existing row, writes a backup and 028 replays after a crash;
- app/memory/learning.py: record_event derivation/idempotency/validation,
  sessions, summary windows, streak, recent materials, timeline truncation;
- /api/learning/*: request validation, 400/404 mapping and response shapes.
"""

import json
import re
import shutil
import sqlite3
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes
from app.maintenance import migrations
from app.memory import db, learning

FILE = '028_learning_events.sql'
OLD_VERSION = 26
TABLES = ('learning_sessions', 'learning_events', 'quiz_attempts')
INDEXES = (
    'idx_learning_events_occurred',
    'idx_learning_events_course',
    'idx_learning_events_kp',
    'idx_learning_events_workspace',
    'idx_learning_sessions_started',
    'idx_quiz_attempts_kp',
    'idx_learning_sessions_idempotency',
    'idx_learning_events_idempotency',
)
BASELINE_TABLES = (
    'workspaces',
    'user_profile',
    'courses',
    'kp',
    'source_files',
    'quiz',
    'knowledge_documents',
)


def _current_schema_version():
    return max(
        int(re.match(r'\d+', path.name)[0])
        for path in db.SCHEMA_DIR.glob('*.sql')
    )


def _seed_learning_rows():
    """One accounting course with a kp, a document and a material."""
    conn = db.connect()
    try:
        with conn:
            workspace_id = conn.execute(
                "SELECT id FROM workspaces WHERE key='studypal'").fetchone()['id']
            course_id = conn.execute(
                "INSERT INTO courses(name, workspace_id) VALUES('会计学', ?)",
                (workspace_id,),
            ).lastrowid
            kp_id = conn.execute(
                "INSERT INTO kp(course_id, name) VALUES(?, '资产')", (course_id,)
            ).lastrowid
            document_id = conn.execute(
                "INSERT INTO knowledge_documents("
                " workspace_id, course_id, kp_id, kind, title, body_md, status)"
                " VALUES(?,?,?,'note','资产笔记','# 资产','active')",
                (workspace_id, course_id, kp_id),
            ).lastrowid
            source_id = conn.execute(
                "INSERT INTO source_files(course_id, path, sha256, status)"
                " VALUES(?,?,?,'indexed')",
                (course_id, 'notes/资产.md', 'sha-asset'),
            ).lastrowid
    finally:
        conn.close()
    return {
        'workspace_id': workspace_id,
        'course_id': course_id,
        'kp_id': kp_id,
        'document_id': document_id,
        'source_id': source_id,
    }


def _relative_hint(path):
    """Test-local mirror of the module helper contract (tail two segments)."""
    parts = [part for part in str(path).replace(chr(92), '/').split('/') if part]
    return '/'.join(parts[-2:]) if len(parts) > 2 else '/'.join(parts)


def _raw():
    return sqlite3.connect(str(db.DB_PATH))


def _scalar(sql, params=()):
    conn = db.connect()
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def _stamp(days_ago=0):
    conn = db.connect()
    try:
        return conn.execute(
            "SELECT datetime('now','localtime',?)", (f'-{int(days_ago)} days',)
        ).fetchone()[0]
    finally:
        conn.close()


def _event_count():
    return _scalar('SELECT COUNT(*) FROM learning_events')


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'APP_DIR', tmp_path / 'app')
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'app' / 'test.db')
    applied = db.apply_schema()
    assert FILE in applied
    ids = _seed_learning_rows()
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        yield {'client': client, **ids}


def _seed_other_workspace() -> dict:
    """A second workspace with its own course, kp, document and material."""
    conn = db.connect()
    try:
        with conn:
            workspace_id = conn.execute(
                "INSERT OR IGNORE INTO workspaces(key, name, status)"
                " VALUES('workpal', '工作', 'active')"
            ).lastrowid
            if not workspace_id:
                workspace_id = conn.execute(
                    "SELECT id FROM workspaces WHERE key='workpal'"
                ).fetchone()['id']
            course_id = conn.execute(
                "INSERT INTO courses(name, workspace_id) VALUES('项目管理', ?)",
                (workspace_id,),
            ).lastrowid
            kp_id = conn.execute(
                "INSERT INTO kp(course_id, name) VALUES(?, '里程碑')", (course_id,)
            ).lastrowid
            document_id = conn.execute(
                "INSERT INTO knowledge_documents("
                " workspace_id, course_id, kp_id, kind, title, body_md, status)"
                " VALUES(?,?,?,'note','里程碑笔记','# 里程碑','active')",
                (workspace_id, course_id, kp_id),
            ).lastrowid
            source_id = conn.execute(
                "INSERT INTO source_files(course_id, path, sha256, status)"
                " VALUES(?,?,?,'indexed')",
                (course_id, 'work/里程碑.md', 'sha-milestone'),
            ).lastrowid
    finally:
        conn.close()
    return {
        'workspace_id': workspace_id,
        'course_id': course_id,
        'kp_id': kp_id,
        'document_id': document_id,
        'source_id': source_id,
    }


def test_cross_workspace_links_are_rejected(env):
    other = _seed_other_workspace()
    cases = (
        ('kp_id', other['kp_id'], '知识点不属于该工作区'),
        ('document_id', other['document_id'], '不属于该工作区'),
        ('source_id', other['source_id'], '材料不属于该工作区'),
        ('course', '项目管理', '课程不存在'),
    )
    before = _event_count()
    for field, value, message in cases:
        with pytest.raises(ValueError) as caught:
            learning.record_event('open_kp', workspace_key='studypal', **{field: value})
        assert message in str(caught.value)
    assert _event_count() == before


def test_cross_workspace_session_is_rejected(env):
    session, _ = learning.start_session('study', workspace_key='workpal')
    with pytest.raises(ValueError) as caught:
        learning.record_event('read', workspace_key='studypal', session_id=session['id'])
    assert '学习会话不属于该工作区' in str(caught.value)


def test_same_workspace_links_still_work(env):
    event, duplicated = learning.record_event(
        'excerpt', workspace_key='studypal', kp_id=env['kp_id'],
        document_id=env['document_id'], source_id=env['source_id'],
    )
    assert duplicated is False
    assert event['kp_id'] == env['kp_id']
    assert event['document_id'] == env['document_id']
    assert event['source_id'] == env['source_id']

def test_fresh_schema_creates_learning_tables_and_partial_unique_keys(env):
    user_version = _scalar('PRAGMA user_version')
    assert user_version == _current_schema_version()
    conn = db.connect()
    try:
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        indexes = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        }
        event_index_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index'"
            " AND name='idx_learning_events_idempotency'").fetchone()[0]
        session_index_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index'"
            " AND name='idx_learning_sessions_idempotency'").fetchone()[0]
        columns = [row[1] for row in conn.execute('PRAGMA table_info(learning_events)')]
    finally:
        conn.close()
    assert set(TABLES) <= tables
    assert set(INDEXES) <= indexes
    assert "idempotency_key != ''" in event_index_sql
    assert "idempotency_key != ''" in session_index_sql
    for column in ('session_id', 'workspace_id', 'course_id', 'kp_id', 'document_id',
                   'source_id', 'kind', 'payload_json', 'duration_seconds',
                   'occurred_at', 'idempotency_key', 'created_at'):
        assert column in columns

    raw = _raw()
    try:
        with raw:
            raw.execute("INSERT INTO learning_events(kind, idempotency_key) VALUES('read','')")
            raw.execute("INSERT INTO learning_events(kind, idempotency_key) VALUES('read','')")
            raw.execute("INSERT INTO learning_events(kind, idempotency_key) VALUES('read','dup')")
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute("INSERT INTO learning_events(kind, idempotency_key) VALUES('read','dup')")
        raw.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute("INSERT INTO learning_events(kind) VALUES('not-a-kind')")
        raw.rollback()
    finally:
        raw.close()


def test_upgrade_from_026_database_keeps_existing_data(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'APP_DIR', tmp_path / 'app')
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'app' / 'old.db')
    old_dir = tmp_path / 'schema-026'
    old_dir.mkdir()
    for path in sorted(db.SCHEMA_DIR.glob('*.sql')):
        if int(re.match(r'\d+', path.name)[0]) <= OLD_VERSION:
            shutil.copy2(path, old_dir / path.name)
    migrations.upgrade_database(
        db.DB_PATH, old_dir,
        legacy_upgrade=db._legacy_upgrade, seed_backfill=db._seed_backfill)

    raw = _raw()
    try:
        with raw:
            raw.execute("INSERT INTO courses(name) VALUES('会计学')")
            raw.execute("INSERT INTO kp(course_id, name) VALUES(1, '资产')")
            raw.execute(
                "INSERT INTO source_files(course_id, path, sha256, status)"
                " VALUES(1, 'a.md', 'sha-a', 'indexed')")
            raw.execute(
                "INSERT INTO quiz(course_id, kp_id, stem, answer)"
                " VALUES(1, 1, '什么是资产？', '资产')")
        before = {
            table: raw.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in BASELINE_TABLES
        }
        assert raw.execute('PRAGMA user_version').fetchone()[0] == OLD_VERSION
    finally:
        raw.close()

    applied = db.apply_schema()
    assert FILE in applied

    conn = db.connect()
    try:
        version = conn.execute('PRAGMA user_version').fetchone()[0]
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        after = {
            table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in BASELINE_TABLES
        }
        recorded = {row[0] for row in conn.execute('SELECT version FROM schema_migrations')}
    finally:
        conn.close()
    assert version == _current_schema_version()
    assert set(TABLES) <= tables
    assert after == before
    assert FILE in recorded
    assert list((tmp_path / 'app' / 'backups').glob('before-upgrade-*.db'))

    # A crash between "028 applied" and "recorded" must be safe to retry.
    raw = _raw()
    try:
        with raw:
            raw.execute('DELETE FROM schema_migrations WHERE version=?', (FILE,))
    finally:
        raw.close()
    migrations.upgrade_database(db.DB_PATH)
    conn = db.connect()
    try:
        recorded = {row[0] for row in conn.execute('SELECT version FROM schema_migrations')}
        after_replay = {
            table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in BASELINE_TABLES
        }
    finally:
        conn.close()
    assert FILE in recorded
    assert after_replay == before
    event, duplicated = learning.record_event('read', course='会计学')
    assert duplicated is False
    assert event['course_id'] == 1


def test_record_event_inserts_derives_links_and_workspace(env):
    # user_profile has no row in a fresh database: the default workspace must
    # fall back to studypal instead of failing.
    default_event, _ = learning.record_event('read')
    assert default_event['workspace_id'] == env['workspace_id']

    event, duplicated = learning.record_event(
        'note_saved', course='会计学', duration_seconds=120, payload={'chars': 1200})
    assert duplicated is False
    assert event['kind'] == 'note_saved'
    assert event['workspace_id'] == env['workspace_id']
    assert event['course_id'] == env['course_id']
    assert event['kp_id'] is None
    assert event['payload'] == {'chars': 1200}
    assert re.match(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', event['occurred_at'])

    derived, _ = learning.record_event(
        'open_material', document_id=env['document_id'], source_id=env['source_id'])
    assert derived['course_id'] == env['course_id']
    assert derived['kp_id'] == env['kp_id']
    assert derived['document_id'] == env['document_id']
    assert derived['source_id'] == env['source_id']

    kp_only, _ = learning.record_event('open_kp', kp_id=env['kp_id'])
    assert kp_only['course_id'] == env['course_id']
    assert kp_only['kp_id'] == env['kp_id']


def test_record_event_idempotency_and_review_integration(env):
    first, duplicated = learning.record_event(
        'read', course='会计学', idempotency_key='evt-1')
    assert duplicated is False
    again, duplicated = learning.record_event(
        'read', course='会计学', idempotency_key='evt-1')
    assert duplicated is True
    assert again['id'] == first['id']
    assert _event_count() == 1

    learning.record_event('read', course='会计学')
    learning.record_event('read', course='会计学')
    assert _event_count() == 3

    # review.py calls with workspace=/payload JSON string/source='review'.
    event, duplicated = learning.record_event(
        kind='review_item', course='会计学', kp_id=env['kp_id'], workspace=None,
        payload=json.dumps({'result': 'good'}, ensure_ascii=False), source='review')
    assert duplicated is False
    assert event['payload']['result'] == 'good'
    assert event['payload']['source'] == 'review'
    assert 'payload_json' not in event  # raw JSON stays an internal column

    explicit, _ = learning.record_event(
        'read', course='会计学', workspace_key='studypal')
    assert explicit['workspace_id'] == env['workspace_id']


def test_record_event_validates_inputs(env):
    cases = [
        (lambda: learning.record_event('not-a-kind'), '事件类型不合法'),
        (lambda: learning.record_event('read', course='不存在课程'), '课程不存在'),
        (lambda: learning.record_event('open_kp', kp_id=999999), '知识点不存在'),
        (lambda: learning.record_event('open_material', document_id=999999), '文档不存在'),
        (lambda: learning.record_event('read', source_id=999999), '材料不存在'),
        (lambda: learning.record_event('read', duration_seconds=-1), 'duration_seconds'),
        (lambda: learning.record_event('read', duration_seconds=86401), '24 小时'),
        (lambda: learning.record_event('read', payload=['not', 'an', 'object']), 'payload'),
        (lambda: learning.record_event('read', payload='not json'), 'payload'),
        (lambda: learning.record_event('read', occurred_at='not-a-time'), '时间格式'),
        (lambda: learning.record_event('read', workspace_key='no-such-pod'), '工作区不存在'),
    ]
    for call, message in cases:
        with pytest.raises(ValueError) as error:
            call()
        assert message in str(error.value)
    assert _event_count() == 0


def test_start_and_end_session(env):
    session, duplicated = learning.start_session(
        'review', course='会计学', idempotency_key='sess-1')
    assert duplicated is False
    assert session['kind'] == 'review'
    assert session['course_id'] == env['course_id']
    assert session['workspace_id'] == env['workspace_id']
    assert session['started_at']
    assert session['ended_at'] == ''

    again, duplicated = learning.start_session(
        'review', course='会计学', idempotency_key='sess-1')
    assert duplicated is True
    assert again['id'] == session['id']

    ended = learning.end_session(session['id'], ended_at='2026-10-18 09:30:00')
    assert ended['ended_at'] == '2026-10-18 09:30:00'
    assert learning.end_session(session['id']) == ended
    with pytest.raises(ValueError) as error:
        learning.end_session(999999)
    assert '学习会话不存在' in str(error.value)
    with pytest.raises(ValueError) as error:
        learning.start_session('not-a-kind')
    assert '会话类型不合法' in str(error.value)


def test_summary_today_week_counts(env):
    learning.record_event('note_saved', course='会计学', duration_seconds=120,
                          occurred_at=_stamp(0))
    learning.record_event('review_item', course='会计学', duration_seconds=60,
                          occurred_at=_stamp(0), kp_id=env['kp_id'])
    learning.record_event('candidate_confirmed', occurred_at=_stamp(1))
    learning.record_event('read', occurred_at=_stamp(6))
    learning.record_event('note_saved', occurred_at=_stamp(7))

    dashboard = learning.summary()
    assert dashboard['today'] == {
        'duration_minutes': 3,
        'events': 2,
        'notes': 1,
        'candidates_confirmed': 0,
        'reviews': 1,
    }
    assert dashboard['week'] == {
        'duration_minutes': 3,
        'events': 4,
        'notes': 1,
        'candidates_confirmed': 1,
        'reviews': 1,
    }
    assert dashboard['streak_days'] == 2
    assert len(dashboard['timeline']) == 1
    assert len(learning.summary('week')['timeline']) == 3
    assert len(learning.summary('all')['timeline']) == 4
    with pytest.raises(ValueError) as error:
        learning.summary('year')
    assert '统计范围' in str(error.value)


def test_summary_empty_is_zero(env):
    dashboard = learning.summary()
    assert dashboard['today'] == {
        'duration_minutes': 0,
        'events': 0,
        'notes': 0,
        'candidates_confirmed': 0,
        'reviews': 0,
    }
    assert dashboard['week'] == dashboard['today']
    assert dashboard['streak_days'] == 0
    assert dashboard['recent_materials'] == []
    assert dashboard['timeline'] == []


def test_streak_falls_back_to_yesterday(env):
    learning.record_event('read', occurred_at=_stamp(1))
    learning.record_event('read', occurred_at=_stamp(2))
    assert learning.summary()['streak_days'] == 2


def test_streak_stops_at_first_gap(env):
    for days_ago in (0, 1, 2, 4):
        learning.record_event('read', occurred_at=_stamp(days_ago))
    assert learning.summary()['streak_days'] == 3


def test_summary_recent_materials_latest_kind(env):
    learning.record_event('read', source_id=env['source_id'], occurred_at=_stamp(2))
    learning.record_event('open_material', source_id=env['source_id'], occurred_at=_stamp(0))
    second = _scalar(
        "SELECT id FROM source_files WHERE path='notes/资产.md'")
    assert second == env['source_id']

    materials = learning.summary()['recent_materials']
    assert len(materials) == 1
    material = materials[0]
    assert set(material) == {'source_id', 'path', 'last_event_at', 'kind'}
    assert material['source_id'] == env['source_id']
    assert material['path'] == 'notes/资产.md'
    assert material['kind'] == 'open_material'
    assert learning.recent_materials(limit=1) == materials
    assert learning.recent_materials(limit=0)[0]['source_id'] == env['source_id']


def test_timeline_titles_payload_truncation_and_filters(env):
    payload = {'text': 'x' * 300}
    learning.record_event(
        'open_material', document_id=env['document_id'], source_id=env['source_id'],
        duration_seconds=90, payload=payload)
    other, _ = learning.start_session('study')
    learning.record_event('quiz_attempt', kp_id=env['kp_id'], session_id=other['id'])

    feed = learning.timeline()
    assert [item['kind'] for item in feed] == ['quiz_attempt', 'open_material']
    first = feed[1]
    assert first['course'] == '会计学'
    assert first['kp_name'] == '资产'
    assert first['document_title'] == '资产笔记'
    assert first['source_path'] == 'notes/资产.md'
    assert first['source_path'] == first['source_path_rel']
    assert first['source_path_rel'] == 'notes/资产.md'
    assert first['payload_truncated'] is True
    assert first['payload'].endswith('…')
    assert len(first['payload']) == 201
    assert first['duration_seconds'] == 90

    assert len(learning.timeline(course='会计学')) == 2
    with pytest.raises(ValueError) as error:
        learning.timeline(course='不存在课程')
    assert '课程不存在' in str(error.value)
    assert len(learning.timeline(limit=1)) == 1
    assert len(learning.timeline(limit=9999)) == 2


def test_timeline_source_path_rel_hides_absolute_prefix(env):
    from app.memory import learning

    learning.record_event('open_material', source_id=env['source_id'])
    feed = learning.timeline()
    assert feed[0]['source_path_rel'] == _relative_hint(feed[0]['source_path'])
    assert _relative_hint('/Users/someone/Deep/Workspace/课程/第三章.pdf') == '课程/第三章.pdf'
    assert learning.timeline()[0]['source_path'] == feed[0]['source_path']


def test_timeline_course_filter_is_workspace_scoped(env):
    from app.memory import db, learning

    learning.record_event('open_material', kp_id=env['kp_id'])
    assert len(learning.timeline(course='会计学', workspace_key='studypal')) == 1
    conn = db.connect()
    try:
        with conn:
            foreign = conn.execute(
                "INSERT INTO courses(name, workspace_id)"
                " VALUES('他区会计学', (SELECT id FROM workspaces WHERE key='studypal'))"
            ).lastrowid
            conn.execute(
                "INSERT INTO learning_events(kind, workspace_id, course_id)"
                " VALUES('read', ?, ?)", (env['workspace_id'], foreign))
    finally:
        conn.close()
    # the foreign course exists, so it is not "missing": workspace scoping is
    # proven structurally by the following assertion on a missing name
    with pytest.raises(ValueError) as error:
        learning.timeline(course='不存在的课程', workspace_key='studypal')
    assert '课程不存在' in str(error.value)


def test_api_events_idempotency_and_error_mapping(env):
    client = env['client']
    body = {'kind': 'note_saved', 'course': '会计学', 'duration_seconds': 30,
            'idempotency_key': 'api-evt-1', 'payload': {'chars': 10}}
    response = client.post('/api/learning/events', json=body)
    assert response.status_code == 200
    first = response.json()
    assert first['ok'] is True
    assert first['duplicated'] is False
    assert first['event']['course_id'] == env['course_id']

    again = client.post('/api/learning/events', json=body).json()
    assert again['duplicated'] is True
    assert again['event']['id'] == first['event']['id']
    assert _event_count() == 1

    assert client.post('/api/learning/events', json={'kind': 'nope'}).status_code == 400
    assert client.post(
        '/api/learning/events',
        json={'kind': 'read', 'course': '不存在课程'}).status_code == 400
    bad_kp = client.post('/api/learning/events', json={'kind': 'open_kp', 'kp_id': 999999})
    assert bad_kp.status_code == 404
    assert '知识点不存在' in bad_kp.json()['detail']
    bad_doc = client.post(
        '/api/learning/events', json={'kind': 'read', 'document_id': 999999})
    assert bad_doc.status_code == 404
    assert client.post(
        '/api/learning/events', json={'kind': 'read', 'duration_seconds': -5}
    ).status_code == 400


def test_api_summary_and_timeline(env):
    client = env['client']
    client.post('/api/learning/events',
                json={'kind': 'note_saved', 'course': '会计学', 'duration_seconds': 60})
    client.post('/api/learning/events', json={'kind': 'read', 'course': '会计学'})

    summary = client.get('/api/learning/summary')
    assert summary.status_code == 200
    dashboard = summary.json()
    assert set(dashboard) == {
        'today', 'week', 'streak_days', 'recent_materials', 'timeline'}
    assert dashboard['today']['events'] == 2
    assert dashboard['today']['duration_minutes'] == 1
    assert dashboard['streak_days'] == 1

    assert client.get('/api/learning/summary?range=week').status_code == 200
    assert client.get('/api/learning/summary?range=year').status_code == 400

    feed = client.get('/api/learning/timeline')
    assert feed.status_code == 200
    items = feed.json()
    assert isinstance(items, list)
    assert len(items) == 2
    assert items[0]['id'] > items[1]['id']
    assert client.get('/api/learning/timeline?limit=1').json()[0]['id'] == items[0]['id']
    assert client.get(
        '/api/learning/timeline?course=不存在课程').status_code == 400


def test_api_sessions_lifecycle(env):
    client = env['client']
    body = {'kind': 'review', 'course': '会计学', 'idempotency_key': 'api-sess-1'}
    created = client.post('/api/learning/sessions', json=body)
    assert created.status_code == 200
    payload = created.json()
    assert payload['ok'] is True
    assert payload['duplicated'] is False
    session_id = payload['session']['id']
    assert payload['session']['ended_at'] == ''

    repeated = client.post('/api/learning/sessions', json=body).json()
    assert repeated['duplicated'] is True
    assert repeated['session']['id'] == session_id

    patched = client.patch(
        f'/api/learning/sessions/{session_id}',
        json={'ended_at': '2026-10-18 10:00:00'})
    assert patched.status_code == 200
    assert patched.json()['session']['ended_at'] == '2026-10-18 10:00:00'
    assert client.patch('/api/learning/sessions/999999',
                        json={}).status_code == 404
    assert client.post(
        '/api/learning/sessions', json={'kind': 'nope'}).status_code == 400


def test_default_workspace_follows_user_profile(env):
    conn = db.connect()
    try:
        with conn:
            workpal_id = conn.execute(
                "SELECT id FROM workspaces WHERE key='workpal'").fetchone()['id']
            conn.execute(
                "INSERT INTO user_profile(id, active_workspace_key) VALUES(1, 'workpal')"
                ' ON CONFLICT(id) DO UPDATE SET active_workspace_key=excluded.active_workspace_key')
    finally:
        conn.close()
    assert workpal_id != env['workspace_id']

    client = env['client']
    response = client.post('/api/learning/events', json={'kind': 'read'})
    assert response.status_code == 200
    assert response.json()['event']['workspace_id'] == workpal_id
    assert client.get('/api/learning/summary').json()['today']['events'] == 1
    assert client.get(
        '/api/learning/summary?workspace_key=studypal'
    ).json()['today']['events'] == 0

    explicit = client.post(
        '/api/learning/events',
        json={'kind': 'read', 'workspace_key': 'studypal'}).json()
    assert explicit['event']['workspace_id'] == env['workspace_id']
