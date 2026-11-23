"""0.6.2 旁听转录 v0（029）：schema、lecture 模块与 /api/asr|lectures 契约。

覆盖：
- 029 建表/索引/CHECK/部分唯一幂等索引，空 key 可复用；
- /api/asr/status 形状、模型下载（后台线程 + 忙碌 409 + 未知模型 400）；
- 会话创建（默认标题、课程校验、幂等）、列表、详情、丢弃；
- 推流：引擎缺失 503、模型缺失 409、正常路径落字幕（假 StreamSession）；
- 结束沉淀：课堂记录文档（带时间戳）+ 学习事件 note_saved，无字幕时不建文档。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes
from app.memory import asr, db, lecture

FILE = '029_lectures.sql'
TABLES = ('lecture_sessions', 'lecture_segments')
INDEXES = (
    'idx_lecture_sessions_workspace',
    'idx_lecture_sessions_course',
    'idx_lecture_segments_session',
    'idx_lecture_sessions_idempotency',
)


def _seed_rows():
    conn = db.connect()
    try:
        with conn:
            workspace_id = conn.execute(
                "SELECT id FROM workspaces WHERE key='studypal'").fetchone()['id']
            course_id = conn.execute(
                "INSERT INTO courses(name, workspace_id) VALUES('会计学', ?)",
                (workspace_id,),
            ).lastrowid
            conn.execute(
                "INSERT INTO workspaces(key,name) VALUES('otherspace','其它工作区')")
    finally:
        conn.close()
    return {'workspace_id': workspace_id, 'course_id': course_id}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'APP_DIR', tmp_path / 'app')
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'app' / 'test.db')
    applied = db.apply_schema()
    assert FILE in applied
    lecture.reset_runtime_state()
    ids = _seed_rows()
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        yield {'client': client, **ids}
    lecture.reset_runtime_state()


# --- schema ---------------------------------------------------------------


def test_schema_tables_checks_and_idempotency(env):
    conn = db.connect()
    try:
        for name in TABLES:
            assert conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (name,)).fetchone()
        for name in INDEXES:
            assert conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
                (name,)).fetchone()
        # CHECK: 来源与状态白名单
        with pytest.raises(Exception):
            conn.execute("INSERT INTO lecture_sessions(source) VALUES('bogus')")
        conn.rollback()
        with pytest.raises(Exception):
            conn.execute("INSERT INTO lecture_sessions(status) VALUES('bogus')")
        conn.rollback()
        # 部分唯一索引：空 key 可复用，非空 key 只能一次
        with conn:
            conn.execute("INSERT INTO lecture_sessions(title) VALUES('a')")
            conn.execute("INSERT INTO lecture_sessions(title) VALUES('b')")
        with pytest.raises(Exception):
            conn.execute(
                "INSERT INTO lecture_sessions(title,idempotency_key) VALUES('c','k1')")
            conn.execute(
                "INSERT INTO lecture_sessions(title,idempotency_key) VALUES('d','k1')")
        conn.rollback()
        # 片段：同一会话 seq 唯一，删会话级联
        with conn:
            conn.execute("INSERT INTO lecture_segments(session_id,seq,start_ms,end_ms,text)"
                         " VALUES(1,0,0,100,'甲')")
        with pytest.raises(Exception):
            conn.execute("INSERT INTO lecture_segments(session_id,seq,text) VALUES(1,0,'乙')")
        conn.rollback()
    finally:
        conn.close()


# --- /api/asr/status 与模型下载 -------------------------------------------


def test_asr_status_shape(env):
    payload = env['client'].get('/api/asr/status').json()
    assert set(payload) == {'engine', 'models', 'download'}
    assert payload['engine']['available'] in (True, False)
    assert 'pip install -r requirements-extensions.txt' in payload['engine']['install_hint']
    assert set(payload['models']) == {'turbo', 'small'}
    assert payload['models']['turbo']['name'] == 'turbo'
    assert payload['download'] is None


def test_download_starts_in_background_and_reports_busy(env, monkeypatch):
    calls = []
    started = threading.Event()

    def fake_download(name, progress=None, **kwargs):
        calls.append(name)
        started.set()
        if progress:
            progress({'file': 'model.bin', 'downloaded': 50, 'total': 100, 'percent': 50.0})
        time.sleep(0.05)

    monkeypatch.setattr(lecture.asr, 'download_model', fake_download)
    first = env['client'].post('/api/asr/models/turbo/download', json={})
    assert first.status_code == 200, first.text
    assert first.json()['started'] is True
    assert started.wait(2.0)
    # 下载中的第二次请求 409（线程可能已结束，故允许 200）
    second = env['client'].post('/api/asr/models/small/download', json={})
    assert second.status_code in (200, 409), second.text
    if second.status_code == 409:
        assert '下载中' in second.json()['detail']
    deadline = time.time() + 2.0
    while time.time() < deadline and lecture.download_state()['state'] != 'done':
        time.sleep(0.01)
    state = lecture.download_state()
    assert state['state'] == 'done' and state['percent'] == 100.0
    assert calls[0] == 'turbo'


def test_download_rejects_unknown_model(env):
    r = env['client'].post('/api/asr/models/giant/download', json={})
    assert r.status_code == 400
    assert '不支持的语音模型' in r.json()['detail']


# --- 会话 -----------------------------------------------------------------


def test_create_session_defaults_title_and_idempotency(env):
    client = env['client']
    body = {'workspace_key': 'studypal', 'course': '会计学'}
    first = client.post('/api/lectures', json=body)
    assert first.status_code == 200, first.text
    session = first.json()['session']
    assert session['status'] == 'live'
    assert session['source'] == 'mic'
    assert session['model'] == 'small'
    assert session['course'] == '会计学'
    assert session['title'].endswith('会计学 旁听')
    assert session['segment_count'] == 0

    keyed = {'workspace_key': 'studypal', 'idempotency_key': 'lecture-a'}
    one = client.post('/api/lectures', json=keyed).json()
    two = client.post('/api/lectures', json=keyed).json()
    assert one['duplicated'] is False
    assert two['duplicated'] is True
    assert two['session']['id'] == one['session']['id']


def test_create_session_rejects_unknown_course_source_and_model(env):
    client = env['client']
    bad = client.post('/api/lectures', json={'course': '不存在的课'})
    assert bad.status_code == 400 and '课程不存在' in bad.json()['detail']
    bad = client.post('/api/lectures', json={'source': 'telepathy'})
    assert bad.status_code == 400 and '声音来源不合法' in bad.json()['detail']
    bad = client.post('/api/lectures', json={'model': 'giant'})
    assert bad.status_code == 400 and '不支持的语音模型' in bad.json()['detail']


def test_list_and_get_session(env):
    client = env['client']
    sid = client.post('/api/lectures', json={}).json()['session']['id']
    items = client.get('/api/lectures', params={'workspace_key': 'studypal'}).json()['items']
    assert [item['id'] for item in items] == [sid]
    assert items[0]['preview'] == ''
    # 其它工作区看不到这条会话
    other = client.get('/api/lectures', params={'workspace_key': 'otherspace'}).json()['items']
    assert other == []
    detail = client.get(f'/api/lectures/{sid}').json()
    assert detail['session']['id'] == sid and detail['segments'] == []
    missing = client.get('/api/lectures/9999')
    assert missing.status_code == 404 and '旁听会话不存在' in missing.json()['detail']


# --- 推流 -----------------------------------------------------------------


def test_chunks_without_engine_is_503(env, monkeypatch):
    client = env['client']
    sid = client.post('/api/lectures', json={}).json()['session']['id']
    monkeypatch.setattr(lecture.asr, 'engine_available', lambda: False)
    r = client.post(f'/api/lectures/{sid}/chunks', content=b'\x00\x01' * 16000,
                    headers={'Content-Type': 'application/octet-stream'})
    assert r.status_code == 503
    assert 'pip install -r requirements-extensions.txt' in r.json()['detail']


def test_chunks_without_model_is_409(env, monkeypatch):
    client = env['client']
    sid = client.post('/api/lectures', json={}).json()['session']['id']
    monkeypatch.setattr(lecture.asr, 'engine_available', lambda: True)
    monkeypatch.setattr(lecture.asr, 'model_state',
                        lambda name: {'name': name, 'present': False, 'missing_files': ['model.bin']})
    r = client.post(f'/api/lectures/{sid}/chunks', content=b'\x00\x01' * 16000)
    assert r.status_code == 409
    assert '尚未下载' in r.json()['detail']


class _FakeStream:
    def __init__(self, name):
        self.name = name
        self.fed = 0

    def feed(self, pcm):
        self.fed += int(pcm.shape[0])
        return [asr.Segment(start=self.fed - 16000, end=self.fed,
                            text='今天讲会计的基本假设', confidence=0.9)]

    def flush(self):
        return [asr.Segment(start=self.fed, end=self.fed + 500, text='下课', confidence=0.8)]


@pytest.fixture
def fake_engine(env, monkeypatch):
    monkeypatch.setattr(lecture.asr, 'engine_available', lambda: True)
    monkeypatch.setattr(lecture.asr, 'model_state',
                        lambda name: {'name': name, 'present': True, 'missing_files': []})
    monkeypatch.setattr(lecture.asr, 'StreamSession', _FakeStream)
    return env


def test_chunks_store_segments_and_finalize_makes_document(fake_engine):
    client = fake_engine['client']
    sid = client.post('/api/lectures', json={'course': '会计学'}).json()['session']['id']
    r = client.post(f'/api/lectures/{sid}/chunks', content=b'\x00\x01' * 16000)
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload['duration_ms'] == 1000
    assert payload['segments'][0]['seq'] == 0
    assert payload['segments'][0]['text'] == '今天讲会计的基本假设'
    assert payload['segments'][0]['end_ms'] == 16000

    detail = client.get(f'/api/lectures/{sid}').json()
    assert detail['session']['segment_count'] == 1

    final = client.post(f'/api/lectures/{sid}/finalize', json={})
    assert final.status_code == 200, final.text
    body = final.json()
    assert body['session']['status'] == 'ended'
    assert body['document']['id']
    assert body['segment_count'] == 2  # 本批 + flush 的「下课」
    document = db.connect().execute(
        'SELECT * FROM knowledge_documents WHERE id=?', (body['document']['id'],)).fetchone()
    assert document['kind'] == 'note'
    assert '## 课堂记录' in document['body_md']
    assert '今天讲会计的基本假设' in document['body_md']
    assert '**[00:00:00]**' in document['body_md']
    assert document['course_id'] == fake_engine['course_id']
    tags = db.connect().execute(
        "SELECT t.name FROM entity_tags e JOIN tags t ON t.id=e.tag_id"
        " WHERE e.entity_type='document' AND e.entity_id=?",
        (body['document']['id'],)).fetchall()
    assert [row['name'] for row in tags] == ['旁听']

    event = db.connect().execute(
        "SELECT * FROM learning_events WHERE kind='note_saved'").fetchone()
    assert event is not None
    assert event['document_id'] == body['document']['id']
    assert json.loads(event['payload_json'])['source'] == 'lecture'

    # 再次 finalize 幂等：不重复建文档
    again = client.post(f'/api/lectures/{sid}/finalize', json={}).json()
    assert again == body
    assert db.connect().execute(
        'SELECT COUNT(*) AS n FROM knowledge_documents').fetchone()['n'] == 1


def test_finalize_without_segments_makes_no_document(env):
    client = env['client']
    sid = client.post('/api/lectures', json={}).json()['session']['id']
    body = client.post(f'/api/lectures/{sid}/finalize', json={}).json()
    assert body['session']['status'] == 'ended'
    assert body['document'] is None
    assert db.connect().execute(
        'SELECT COUNT(*) AS n FROM knowledge_documents').fetchone()['n'] == 0


def test_chunks_rejects_empty_and_ended(env):
    client = env['client']
    sid = client.post('/api/lectures', json={}).json()['session']['id']
    empty = client.post(f'/api/lectures/{sid}/chunks', content=b'')
    assert empty.status_code == 400 and '音频块为空' in empty.json()['detail']
    client.post(f'/api/lectures/{sid}/finalize', json={})
    ended = client.post(f'/api/lectures/{sid}/chunks', content=b'\x00\x01' * 160)
    assert ended.status_code == 400 and '已结束' in ended.json()['detail']


# --- 丢弃 -----------------------------------------------------------------


def test_discard_clears_segments_and_listing(fake_engine):
    client = fake_engine['client']
    sid = client.post('/api/lectures', json={}).json()['session']['id']
    client.post(f'/api/lectures/{sid}/chunks', content=b'\x00\x01' * 16000)
    r = client.delete(f'/api/lectures/{sid}')
    assert r.json() == {'ok': True, 'deleted': True}
    assert client.get('/api/lectures').json()['items'] == []
    assert client.get(f'/api/lectures/{sid}').json()['segments'] == []
    row = db.connect().execute(
        'SELECT status FROM lecture_sessions WHERE id=?', (sid,)).fetchone()
    assert row['status'] == 'discarded'
    # 丢弃后不能结束
    late = client.post(f'/api/lectures/{sid}/finalize', json={})
    assert late.status_code == 400 and '已丢弃' in late.json()['detail']
    # 二次丢弃不报错
    assert client.delete(f'/api/lectures/{sid}').json()['deleted'] is False


# --- 0.6.4: deterministic lifecycle races and retry checkpoints ------------


class _OnceStream(_FakeStream):
    def __init__(self, name):
        super().__init__(name)
        self.flush_calls = 0

    def flush(self):
        self.flush_calls += 1
        return super().flush() if self.flush_calls == 1 else []


def _recording_session(monkeypatch, stream_type=_OnceStream):
    monkeypatch.setattr(lecture.asr, 'StreamSession', stream_type)
    sid = lecture.create_session(course='会计学')['session']['id']
    lecture.feed(sid, b'\x00\x01' * 16000)
    return sid, lecture._streams[sid]['stream']


def _rows(sql, args=()):
    conn = db.connect()
    try:
        return [dict(row) for row in conn.execute(sql, args)]
    finally:
        conn.close()


def _sql(sql):
    conn = db.connect()
    try:
        with conn:
            conn.execute(sql)
    finally:
        conn.close()


def _assert_one_publication(sid, result):
    docs = _rows('SELECT id,version,body_md FROM knowledge_documents')
    revisions = _rows('SELECT document_id,version FROM document_revisions')
    events = _rows('SELECT document_id,payload_json FROM learning_events')
    assert len(docs) == len(revisions) == len(events) == 1
    did = result['document']['id']
    assert docs[0]['id'] == revisions[0]['document_id'] == events[0]['document_id'] == did
    assert result['session']['document_id'] == did
    assert docs[0]['version'] == revisions[0]['version'] == 1
    assert json.loads(events[0]['payload_json'])['lecture_session_id'] == sid


def _start_call(name, call):
    outcome = {}

    def run():
        try:
            outcome['result'] = call()
        except BaseException as exc:
            outcome['error'] = exc

    thread = threading.Thread(name=name, target=run, daemon=True)
    thread.start()
    return thread, outcome


def _join_call(task):
    thread, outcome = task
    thread.join(timeout=5)
    assert not thread.is_alive(), 'lifecycle operation deadlocked'
    if 'error' in outcome:
        raise outcome['error']
    return outcome['result']


def _observe_operation_attempt(sid, thread_name, reached):
    # A lock probe fixes the contender's scheduling after the repair. On the
    # old code, the test's database/document probe marks the unsafe progress.
    registry = getattr(lecture, '_session_locks', None)
    if registry is None:
        return
    underlying = registry[sid]

    class ObservedLock:
        def __enter__(self):
            if threading.current_thread().name == thread_name:
                reached.set()
            underlying.acquire()
            return self

        def __exit__(self, *args):
            underlying.release()

    registry[sid] = ObservedLock()


def test_concurrent_finalize_publishes_once_and_returns_identical_results(fake_engine, monkeypatch):
    sid, stream = _recording_session(monkeypatch)
    creating, release, contender = threading.Event(), threading.Event(), threading.Event()
    create, fetch = lecture.documents.create, lecture._fetch_row
    _observe_operation_attempt(sid, 'second-finalize', contender)

    def paused_create(*args, **kwargs):
        if threading.current_thread().name == 'first-finalize':
            creating.set()
            assert release.wait(5)
        return create(*args, **kwargs)

    def observed_fetch(*args):
        row = fetch(*args)
        if threading.current_thread().name == 'second-finalize':
            contender.set()
        return row

    monkeypatch.setattr(lecture.documents, 'create', paused_create)
    monkeypatch.setattr(lecture, '_fetch_row', observed_fetch)
    first = _start_call('first-finalize', lambda: lecture.finalize(sid))
    assert creating.wait(5)
    second = _start_call('second-finalize', lambda: lecture.finalize(sid))
    try:
        assert contender.wait(5)
    finally:
        release.set()
    one, two = _join_call(first), _join_call(second)
    _assert_one_publication(sid, one)
    assert one == two
    assert stream.flush_calls == 1


@pytest.mark.parametrize('boundary', ['live_check', 'segment_store'])
@pytest.mark.parametrize('terminal', ['finalize', 'discard'])
def test_feed_and_terminal_operation_share_entire_lifecycle_lock(
        fake_engine, monkeypatch, boundary, terminal):
    sid, _stream = _recording_session(monkeypatch)
    paused, release, contender = threading.Event(), threading.Event(), threading.Event()
    _observe_operation_attempt(sid, 'terminal', contender)
    original = getattr(lecture, '_live_row' if boundary == 'live_check' else '_store_segments')

    def paused_feed(*args, **kwargs):
        result = original(*args, **kwargs) if boundary == 'live_check' else None
        if threading.current_thread().name == 'feeding':
            paused.set()
            assert release.wait(5)
        return result if boundary == 'live_check' else original(*args, **kwargs)

    monkeypatch.setattr(lecture, '_live_row' if boundary == 'live_check' else '_store_segments', paused_feed)
    if terminal == 'finalize':
        create = lecture.documents.create

        def observed_create(*args, **kwargs):
            contender.set()
            assert release.wait(5)
            return create(*args, **kwargs)

        monkeypatch.setattr(lecture.documents, 'create', observed_create)
    else:
        fetch = lecture._fetch_row

        def observed_fetch(*args):
            row = fetch(*args)
            if threading.current_thread().name == 'terminal':
                contender.set()
                assert release.wait(5)
            return row

        monkeypatch.setattr(lecture, '_fetch_row', observed_fetch)
    feeding = _start_call('feeding', lambda: lecture.feed(sid, b'\x00\x01' * 16000))
    assert paused.wait(5)
    ending = _start_call('terminal', lambda: getattr(lecture, terminal)(sid))
    try:
        assert contender.wait(5)
    finally:
        release.set()
    _join_call(feeding)
    result = _join_call(ending)
    detail = lecture.get_session(sid)
    if terminal == 'finalize':
        assert result['segment_count'] == len(detail['segments']) == 3
        assert _rows('SELECT body_md FROM knowledge_documents')[0]['body_md'].count('今天讲') == 2
        _assert_one_publication(sid, result)
    else:
        assert detail['session']['status'] == 'discarded'
        assert detail['segments'] == []
    assert sid not in lecture._streams


def test_terminal_finalize_never_flushes_again(fake_engine, monkeypatch):
    sid, stream = _recording_session(monkeypatch)
    first = lecture.finalize(sid)
    # Even a stale runtime entry must not be touched after the database terminal check.
    lecture._streams[sid] = {'stream': stream, 'model': 'small', 'samples': 0}

    def forbidden_flush():
        pytest.fail('ended session was flushed again')

    monkeypatch.setattr(stream, 'flush', forbidden_flush)
    assert lecture.finalize(sid, title='ignored retry') == first


def test_finalize_flush_failure_is_retryable(fake_engine, monkeypatch):
    class RetryStream(_OnceStream):
        def flush(self):
            if self.flush_calls == 0:
                self.flush_calls += 1
                raise asr.AsrUnavailable('tail unavailable')
            self.flush_calls += 1
            return _FakeStream.flush(self)

    sid, stream = _recording_session(monkeypatch, RetryStream)
    with pytest.raises((asr.AsrUnavailable, lecture.LectureError), match='tail unavailable'):
        lecture.finalize(sid)
    assert lecture.get_session(sid)['session']['status'] == 'live'
    assert lecture._streams[sid]['stream'] is stream
    assert lecture._streams[sid]['samples'] == 16000
    assert _rows('SELECT id FROM knowledge_documents') == []
    result = lecture.finalize(sid)
    assert result['segment_count'] == 2
    assert stream.flush_calls == 2
    _assert_one_publication(sid, result)


@pytest.mark.parametrize('source', ['feed', 'flush'])
def test_segment_write_failure_retains_pending_output_without_duplicates(fake_engine, monkeypatch, source):
    sid, stream = _recording_session(monkeypatch)
    _sql("""CREATE TRIGGER reject_segment BEFORE INSERT ON lecture_segments
            BEGIN SELECT RAISE(FAIL, 'segment write failed'); END""")
    try:
        with pytest.raises(sqlite3.DatabaseError, match='segment write failed'):
            if source == 'feed':
                lecture.feed(sid, b'\x00\x01' * 16000)
            else:
                lecture.finalize(sid)
        assert lecture.get_session(sid)['session']['status'] == 'live'
        assert lecture._streams[sid]['stream'] is stream
    finally:
        _sql('DROP TRIGGER reject_segment')
    result = lecture.finalize(sid)
    expected = 3 if source == 'feed' else 2
    assert result['segment_count'] == expected
    assert [row['seq'] for row in lecture.get_session(sid)['segments']] == list(range(expected))
    assert stream.flush_calls == 1
    assert lecture.finalize(sid) == result
    _assert_one_publication(sid, result)


@pytest.mark.parametrize('source', ['feed', 'flush'])
def test_segment_commit_followed_by_error_does_not_duplicate_on_retry(fake_engine, monkeypatch, source):
    sid, stream = _recording_session(monkeypatch)
    store = lecture._store_segments
    failed = False

    def fail_after_commit(conn, session_id, segments, **kwargs):
        nonlocal failed
        result = store(conn, session_id, segments, **kwargs)
        if segments and not failed:
            failed = True
            raise sqlite3.OperationalError('after segment commit')
        return result

    monkeypatch.setattr(lecture, '_store_segments', fail_after_commit)
    with pytest.raises(sqlite3.OperationalError, match='after segment commit'):
        if source == 'feed':
            lecture.feed(sid, b'\x00\x01' * 16000)
        else:
            lecture.finalize(sid)
    result = lecture.finalize(sid)
    assert result['segment_count'] == (3 if source == 'feed' else 2)
    assert stream.flush_calls == 1
    _assert_one_publication(sid, result)


def test_discard_write_failure_preserves_stream_until_retry(fake_engine, monkeypatch):
    sid, stream = _recording_session(monkeypatch)
    _sql("""CREATE TRIGGER reject_discard BEFORE UPDATE OF status ON lecture_sessions
            WHEN NEW.status='discarded' BEGIN SELECT RAISE(FAIL, 'discard failed'); END""")
    try:
        with pytest.raises(sqlite3.DatabaseError, match='discard failed'):
            lecture.discard(sid)
        assert lecture.get_session(sid)['session']['status'] == 'live'
        assert lecture._streams[sid]['stream'] is stream
        assert lecture.get_session(sid)['session']['segment_count'] == 1
    finally:
        _sql('DROP TRIGGER reject_discard')
    assert lecture.discard(sid) == {'ok': True, 'deleted': True}
    assert sid not in lecture._streams
    assert lecture.get_session(sid)['segments'] == []


@pytest.mark.parametrize('stage', ['document_before', 'document_after', 'event_before', 'event_after', 'session'])
def test_finalize_publication_failures_retry_without_new_document_or_revision(fake_engine, monkeypatch, stage):
    sid, stream = _recording_session(monkeypatch)
    calls = []
    if stage == 'session':
        _sql("""CREATE TRIGGER reject_end BEFORE UPDATE OF status ON lecture_sessions
                WHEN NEW.status='ended' BEGIN SELECT RAISE(FAIL, 'publication failed'); END""")
    else:
        owner = lecture.documents if stage.startswith('document') else lecture.learning
        name = 'create' if stage.startswith('document') else 'record_event'
        original = getattr(owner, name)

        def fail_once(*args, **kwargs):
            calls.append(1)
            if len(calls) > 1:
                return original(*args, **kwargs)
            if stage.endswith('after'):
                original(*args, **kwargs)
            raise ValueError('publication failed')

        monkeypatch.setattr(owner, name, fail_once)
    try:
        with pytest.raises((ValueError, sqlite3.DatabaseError), match='publication failed'):
            lecture.finalize(sid, title='frozen title')
        assert lecture.get_session(sid)['session']['status'] == 'live'
        assert lecture._streams[sid]['stream'] is stream
    finally:
        if stage == 'session':
            _sql('DROP TRIGGER reject_end')
    result = lecture.finalize(sid)
    assert result['segment_count'] == 2
    assert result['session']['title'] == result['document']['title'] == 'frozen title'
    assert stream.flush_calls == 1
    _assert_one_publication(sid, result)
    assert lecture.finalize(sid) == result


def test_unfinished_publication_rejects_new_audio_until_retry(fake_engine, monkeypatch):
    sid, stream = _recording_session(monkeypatch)
    record = lecture.learning.record_event

    def failed_event(*args, **kwargs):
        raise ValueError('retry publication')

    monkeypatch.setattr(lecture.learning, 'record_event', failed_event)
    with pytest.raises(ValueError, match='retry publication'):
        lecture.finalize(sid)
    with pytest.raises(lecture.LectureError):
        lecture.feed(sid, b'\x00\x01' * 16000)
    assert stream.fed == 16000
    monkeypatch.setattr(lecture.learning, 'record_event', record)
    _assert_one_publication(sid, lecture.finalize(sid))


def test_finalize_without_document_has_identical_retry_result(fake_engine, monkeypatch):
    sid, stream = _recording_session(monkeypatch)
    result = lecture.finalize(sid, make_document=False)
    assert lecture.finalize(sid, make_document=True) == result
    assert result['segment_count'] == 2
    assert result['document'] is None
    assert stream.flush_calls == 1
    assert _rows('SELECT id FROM knowledge_documents') == []
    assert _rows('SELECT id FROM learning_events') == []


def test_chunk_id_retries_reuse_receipt_even_after_finalize(fake_engine, monkeypatch):
    client = fake_engine['client']
    monkeypatch.setattr(lecture.asr, 'StreamSession', _OnceStream)
    sid = lecture.create_session()['session']['id']
    path = f'/api/lectures/{sid}/chunks'
    headers = {'X-Lecture-Chunk-Id': 'stable-chunk-1'}
    pcm = b'\x00\x01' * 16000
    first = client.post(path, content=pcm, headers=headers)
    assert first.status_code == 200
    stream = lecture._streams[sid]['stream']
    second = client.post(path, content=pcm, headers=headers)
    assert second.status_code == 200
    assert second.json() == first.json()
    assert first.json()['accepted'] is True
    assert first.json()['chunk_id'] == 'stable-chunk-1'
    assert stream.fed == 16000
    result = lecture.finalize(sid)
    assert client.post(path, content=pcm, headers=headers).json() == first.json()
    assert lecture.finalize(sid) == result
    assert stream.fed == 16000 and stream.flush_calls == 1


def test_chunk_id_conflict_uses_original_bytes_and_is_session_scoped(fake_engine):
    client = fake_engine['client']
    sid = lecture.create_session()['session']['id']
    other = lecture.create_session()['session']['id']
    headers = {'X-Lecture-Chunk-Id': 'shared-id'}
    path = f'/api/lectures/{sid}/chunks'
    pcm = b'\x00\x01' * 16000
    assert client.post(path, content=pcm + b'\x00', headers=headers).status_code == 200
    # Odd trailing bytes are trimmed for PCM, but still count toward identity.
    conflict = client.post(path, content=pcm + b'\x01', headers=headers)
    assert conflict.status_code == 409
    assert client.post(f'/api/lectures/{other}/chunks', content=b'\x01\x00', headers=headers).status_code == 200
    assert lecture._streams[sid]['stream'].fed == 16000
    assert lecture.get_session(sid)['session']['segment_count'] == 1


def test_legacy_chunks_without_id_are_not_deduplicated(fake_engine):
    client = fake_engine['client']
    sid = lecture.create_session()['session']['id']
    for _ in range(2):
        result = client.post(f'/api/lectures/{sid}/chunks', content=b'\x00\x01' * 16000)
        assert result.status_code == 200
        assert 'chunk_id' not in result.json()
    assert lecture.get_session(sid)['session']['segment_count'] == 2
    assert lecture._streams[sid]['stream'].fed == 32000


def test_concurrent_same_chunk_id_feeds_only_once(fake_engine, monkeypatch):
    entered, release, contender = threading.Event(), threading.Event(), threading.Event()

    class PausedStream(_OnceStream):
        def feed(self, pcm):
            entered.set()
            assert release.wait(5)
            return super().feed(pcm)

    monkeypatch.setattr(lecture.asr, 'StreamSession', PausedStream)
    sid = lecture.create_session()['session']['id']
    pcm = b'\x00\x01' * 16000
    first = _start_call('first-chunk', lambda: lecture.feed(sid, pcm, chunk_id='same'))
    try:
        assert entered.wait(5)
        _observe_operation_attempt(sid, 'second-chunk', contender)
        second = _start_call('second-chunk', lambda: lecture.feed(sid, pcm, chunk_id='same'))
        assert contender.wait(5)
    finally:
        release.set()
    one, two = _join_call(first), _join_call(second)
    assert one == two
    assert lecture._streams[sid]['stream'].fed == 16000
    assert lecture.get_session(sid)['session']['segment_count'] == 1


def test_chunk_asr_failure_acknowledges_acceptance_without_refeeding(fake_engine, monkeypatch):
    class FailedFeedStream(_OnceStream):
        def feed(self, pcm):
            self.fed += int(pcm.shape[0])
            raise asr.AsrUnavailable('accepted audio needs finalize')

    client = fake_engine['client']
    monkeypatch.setattr(lecture.asr, 'StreamSession', FailedFeedStream)
    sid = lecture.create_session()['session']['id']
    path = f'/api/lectures/{sid}/chunks'
    headers = {'X-Lecture-Chunk-Id': 'accepted-once'}
    pcm = b'\x00\x01' * 16000
    first = client.post(path, content=pcm, headers=headers)
    assert first.status_code == 503
    assert first.json() == {'detail': 'accepted audio needs finalize',
                            'accepted': True, 'chunk_id': 'accepted-once'}
    assert first.headers.get('X-Lecture-Chunk-Accepted') == '1'
    assert first.headers.get('X-Lecture-Chunk-Id') == 'accepted-once'
    assert client.post(path, content=pcm, headers=headers).json() == first.json()
    assert client.post(path, content=pcm + b'\x00\x00', headers=headers).status_code == 409
    stream = lecture._streams[sid]['stream']
    assert stream.fed == 16000
    assert lecture._streams[sid]['samples'] == 16000
    result = lecture.finalize(sid)
    assert result['segment_count'] == 1
    _assert_one_publication(sid, result)


@pytest.mark.parametrize('finish_first', [False, True])
def test_chunk_storage_failure_retry_gets_receipt_without_refeeding(fake_engine, monkeypatch, finish_first):
    monkeypatch.setattr(lecture.asr, 'StreamSession', _OnceStream)
    sid = lecture.create_session()['session']['id']
    pcm = b'\x00\x01' * 16000
    _sql("""CREATE TRIGGER reject_chunk BEFORE INSERT ON lecture_segments
            BEGIN SELECT RAISE(FAIL, 'chunk storage failed'); END""")
    try:
        with pytest.raises(sqlite3.DatabaseError, match='chunk storage failed'):
            lecture.feed(sid, pcm, chunk_id='pending-write')
    finally:
        _sql('DROP TRIGGER reject_chunk')
    stream = lecture._streams[sid]['stream']
    if finish_first:
        lecture.finalize(sid)
    receipt = lecture.feed(sid, pcm, chunk_id='pending-write')
    assert receipt['accepted'] is True
    assert receipt['segments'][0]['seq'] == 0
    assert receipt['duration_ms'] == 1000
    assert lecture.feed(sid, pcm, chunk_id='pending-write') == receipt
    assert stream.fed == 16000
    assert lecture.finalize(sid)['segment_count'] == 2


def test_chunk_failure_before_acceptance_can_retry_same_id(fake_engine, monkeypatch):
    client = fake_engine['client']
    sid = lecture.create_session()['session']['id']
    path = f'/api/lectures/{sid}/chunks'
    headers = {'X-Lecture-Chunk-Id': 'engine-later'}
    monkeypatch.setattr(lecture.asr, 'engine_available', lambda: False)
    first = client.post(path, content=b'\x00\x01' * 16000, headers=headers)
    assert first.status_code == 503
    assert first.headers.get('X-Lecture-Chunk-Accepted') != '1'
    monkeypatch.setattr(lecture.asr, 'engine_available', lambda: True)
    retry = client.post(path, content=b'\x00\x01' * 16000, headers=headers)
    assert retry.status_code == 200
    assert retry.json()['accepted'] is True
    assert lecture._streams[sid]['stream'].fed == 16000


def test_discard_rejects_previously_successful_chunk_id(fake_engine):
    client = fake_engine['client']
    sid = lecture.create_session()['session']['id']
    path = f'/api/lectures/{sid}/chunks'
    pcm = b'\x00\x01' * 16000
    headers = {'X-Lecture-Chunk-Id': 'discarded-chunk'}
    assert client.post(path, content=pcm, headers=headers).status_code == 200
    lecture.discard(sid)
    retry = client.post(path, content=pcm, headers=headers)
    assert retry.status_code == 400
    assert '已结束' in retry.json()['detail']
    assert sid not in lecture._streams
    assert lecture.get_session(sid)['segments'] == []


@pytest.mark.parametrize('replacement', [b'', b'\x00' * (lecture.MAX_CHUNK_BYTES + 1)], ids=['empty', 'oversized'])
def test_known_chunk_id_conflict_precedes_new_chunk_validation(fake_engine, replacement):
    client = fake_engine['client']
    sid = lecture.create_session()['session']['id']
    path = f'/api/lectures/{sid}/chunks'
    headers = {'X-Lecture-Chunk-Id': 'existing-chunk'}
    assert client.post(path, content=b'\x00\x01' * 160, headers=headers).status_code == 200
    assert client.post(path, content=replacement, headers=headers).status_code == 409


def test_next_chunk_recovers_previous_chunk_receipt(fake_engine, monkeypatch):
    monkeypatch.setattr(lecture.asr, 'StreamSession', _OnceStream)
    sid = lecture.create_session()['session']['id']
    pcm = b'\x00\x01' * 16000
    _sql("""CREATE TRIGGER reject_pending BEFORE INSERT ON lecture_segments
            BEGIN SELECT RAISE(FAIL, 'pending failed'); END""")
    try:
        with pytest.raises(sqlite3.DatabaseError, match='pending failed'):
            lecture.feed(sid, pcm, chunk_id='first')
    finally:
        _sql('DROP TRIGGER reject_pending')
    second = lecture.feed(sid, pcm, chunk_id='second')
    first = lecture.feed(sid, pcm, chunk_id='first')
    assert first['segments'][0]['seq'] == 0
    assert first['duration_ms'] == 1000
    assert second['segments'][0]['seq'] == 1
    assert second['duration_ms'] == 2000
    assert lecture._streams[sid]['stream'].fed == 32000


def test_empty_chunk_receipt_is_cached_and_return_value_cannot_mutate_it(fake_engine, monkeypatch):
    class SilentStream(_OnceStream):
        def feed(self, pcm):
            self.fed += len(pcm)
            return []

    monkeypatch.setattr(lecture.asr, 'StreamSession', SilentStream)
    sid = lecture.create_session()['session']['id']
    pcm = b'\x00\x01' * 160
    first = lecture.feed(sid, pcm, chunk_id='silent')
    first['segments'].append({'text': 'tampered'})
    second = lecture.feed(sid, pcm, chunk_id='silent')
    assert second['segments'] == []
    assert second['accepted'] is True
    assert lecture._streams[sid]['stream'].fed == 160
