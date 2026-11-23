"""O-DATA-2: GET read endpoints must not write (PRAGMA data_version evidence)."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes
from app.ingest import jobs
from app.memory import db, repo


class Monitor:
    """One long-lived connection: other connections' commits bump data_version."""

    def __init__(self, db_path):
        self.connection = sqlite3.connect(str(db_path))

    def data_version(self):
        return self.connection.execute('PRAGMA data_version').fetchone()[0]

    def close(self):
        self.connection.close()


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / 'data'
    db_path = root / 'app' / 'banxue.db'
    monkeypatch.setattr(db, 'DATA_DIR', root)
    monkeypatch.setattr(db, 'APP_DIR', root / 'app')
    monkeypatch.setattr(db, 'DB_PATH', db_path)
    db.apply_schema()

    connection = db.connect()
    try:
        course_id = connection.execute(
            "INSERT INTO courses(name) VALUES('纯净课程')").lastrowid
        node_a = connection.execute(
            "INSERT INTO kp(course_id,name,chapter,gist) VALUES(?,?,?,?)",
            (course_id, '节点甲', '第一章', '要点甲')).lastrowid
        node_b = connection.execute(
            "INSERT INTO kp(course_id,name,chapter,gist) VALUES(?,?,?,?)",
            (course_id, '节点乙', '第二章', '要点乙')).lastrowid
        # A review task with one pending candidate: physically waiting_review.
        review_source = connection.execute(
            "INSERT INTO source_files(course_id,path,sha256,status) VALUES(?,?,?,?)",
            (course_id, '/tmp/review.md', 'sha-review', 'indexed')).lastrowid
        connection.execute(
            "INSERT INTO knowledge_candidates(source_id,schema_version,name,status) VALUES(?,?,?,?)",
            (review_source, 'cyberpal.knowledge.v1', '待确认', 'pending'))
        review_job = connection.execute(
            """INSERT INTO ingest_jobs
               (course_id,workspace_key,path,folder,sha256,source_id,status,stage)
               VALUES(?,?,?,?,?,?, 'waiting_review', 'review')""",
            (course_id, 'studypal', '/tmp/review.md', '/tmp', 'sha-review', review_source)).lastrowid
        # A reviewed task: no pending candidate, GET must report succeeded without writing.
        done_source = connection.execute(
            "INSERT INTO source_files(course_id,path,sha256,status) VALUES(?,?,?,?)",
            (course_id, '/tmp/done.md', 'sha-done', 'indexed')).lastrowid
        done_job = connection.execute(
            """INSERT INTO ingest_jobs
               (course_id,workspace_key,path,folder,sha256,source_id,status,stage)
               VALUES(?,?,?,?,?,?, 'waiting_review', 'review')""",
            (course_id, 'studypal', '/tmp/done.md', '/tmp', 'sha-done', done_source)).lastrowid
        other_course = connection.execute(
            "INSERT INTO courses(name) VALUES('桥接课程')").lastrowid
        bridge_target = connection.execute(
            "INSERT INTO kp(course_id,name,chapter) VALUES(?,?,?)",
            (other_course, '节点丙', '第一章')).lastrowid
        connection.execute(
            "INSERT INTO bridges(course_a,kp_a,course_b,kp_b) VALUES(?,?,?,?)",
            ('纯净课程', '节点甲', '桥接课程', '节点丙'))
        connection.commit()
    finally:
        connection.close()

    monitor = Monitor(db_path)
    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)
    yield {
        'monitor': monitor,
        'client': client,
        'db_path': db_path,
        'course_id': course_id,
        'other_course_id': other_course,
        'node_ids': [node_a, node_b],
        'bridge_target': bridge_target,
        'review_job': review_job,
        'done_job': done_job,
    }
    client.close()
    monitor.close()


def test_ingest_reads_are_pure_and_review_status_is_explicit(world):
    client, db_path = world['client'], world['db_path']
    baseline = world['monitor'].data_version()

    for _ in range(2):
        listing = client.get('/api/ingest/jobs', params={'course': '纯净课程'})
        assert listing.status_code == 200
        assert client.get('/api/ingest/jobs').status_code == 200
        review = client.get(f"/api/ingest/jobs/{world['review_job']}").json()
        done = client.get(f"/api/ingest/jobs/{world['done_job']}").json()

    assert world['monitor'].data_version() == baseline, 'GET /api/ingest/* must not write the database'
    assert review['status'] == 'waiting_review'
    assert done['status'] == 'succeeded'
    assert done['stage'] == 'complete'
    assert {row['id'] for row in listing.json()} == {world['review_job'], world['done_job']}

    connection = sqlite3.connect(str(db_path))
    try:
        raw = dict(connection.execute(
            'SELECT id,status FROM ingest_jobs').fetchall())
    finally:
        connection.close()
    assert raw == {world['review_job']: 'waiting_review', world['done_job']: 'waiting_review'}

    finished = jobs.finish(world['done_job'])
    assert finished['status'] == 'succeeded'
    assert world['monitor'].data_version() != baseline, 'explicit finish() is the write event'
    connection = sqlite3.connect(str(db_path))
    try:
        assert dict(connection.execute('SELECT id,status FROM ingest_jobs').fetchall()) == {
            world['review_job']: 'waiting_review', world['done_job']: 'succeeded'}
    finally:
        connection.close()


def test_graph_reads_are_pure_and_still_resolve_structure(world):
    client, db_path = world['client'], world['db_path']
    baseline = world['monitor'].data_version()

    first = client.get('/api/graph', params={'course': '纯净课程'})
    second = client.get('/api/graph', params={'course': '纯净课程'})
    assert first.status_code == second.status_code == 200
    assert world['monitor'].data_version() == baseline, 'GET /api/graph must not write the database'

    graph = first.json()
    assert {node['id'] for node in graph['nodes']} == set(world['node_ids'])
    assert graph['catalog_status'] == 'ready'
    assert [chapter['title'] for chapter in graph['chapters']] == ['第一章', '第二章']
    assert graph['chapter_counts'] == {chapter['key']: 1 for chapter in graph['chapters']}
    assert {node['chapter_key'] for node in graph['nodes']} == set(graph['chapter_counts'])
    assert all(node['node_key'] for node in graph['nodes'])

    # The read path resolved keys in memory only: stored rows are untouched.
    connection = sqlite3.connect(str(db_path))
    try:
        stored = connection.execute(
            'SELECT chapter_key FROM kp WHERE course_id=? ORDER BY id',
            (world['course_id'],)).fetchall()
        identities = connection.execute('SELECT COUNT(*) FROM kp_identities').fetchone()[0]
    finally:
        connection.close()
    assert [row[0] for row in stored] == ['', '']
    assert identities == 0

    summary = repo.sync_memory_views('纯净课程')
    assert summary['courses'] == 1
    assert summary['identities'] == 3  # identities are global: two nodes plus the bridge target
    assert world['monitor'].data_version() != baseline, 'explicit maintenance is the write event'
    connection = sqlite3.connect(str(db_path))
    try:
        stored = [row[0] for row in connection.execute(
            'SELECT chapter_key FROM kp WHERE course_id=? ORDER BY id',
            (world['course_id'],)).fetchall()]
        identities = connection.execute('SELECT COUNT(*) FROM kp_identities').fetchone()[0]
        node_meta = connection.execute(
            '''SELECT COUNT(*) FROM knowledge_node_meta nm JOIN kp ON kp.id=nm.kp_id
               WHERE kp.course_id=?''', (world['course_id'],)).fetchone()[0]
    finally:
        connection.close()
    assert all(stored)
    assert identities == 3
    assert node_meta == 2

    after = client.get('/api/graph', params={'course': '纯净课程'}).json()
    assert [chapter['title'] for chapter in after['chapters']] == ['第一章', '第二章']
    assert {node['chapter_key'] for node in after['nodes']} == set(stored)


def test_bridge_reads_are_pure_and_resolve_current_nodes(world):
    client, db_path = world['client'], world['db_path']
    baseline = world['monitor'].data_version()

    first = client.get('/api/bridges')
    second = client.get('/api/bridges')
    assert first.status_code == second.status_code == 200
    assert world['monitor'].data_version() == baseline, 'GET /api/bridges must not write the database'
    rows = first.json()
    assert len(rows) == 1
    assert rows[0]['status'] == 'resolved'
    expected_key = repo._node_key(world['course_id'], '节点甲')
    assert rows[0]['a_node_key'] == expected_key

    connection = sqlite3.connect(str(db_path))
    try:
        assert connection.execute('SELECT COUNT(*) FROM bridge_links').fetchone()[0] == 0
    finally:
        connection.close()

    repo.sync_bridge_links()
    assert world['monitor'].data_version() != baseline, 'explicit bridge maintenance is the write event'
    connection = sqlite3.connect(str(db_path))
    try:
        assert connection.execute('SELECT COUNT(*) FROM bridge_links').fetchone()[0] == 1
    finally:
        connection.close()
    assert client.get('/api/bridges').json()[0]['status'] == 'resolved'


def test_concurrent_reads_do_not_raise_lock_errors(world):
    client, db_path = world['client'], world['db_path']
    baseline = world['monitor'].data_version()

    def read(index):
        if index % 3 == 0:
            return client.get('/api/graph', params={'course': '纯净课程'}).status_code
        if index % 3 == 1:
            return client.get('/api/ingest/jobs', params={'course': '纯净课程'}).status_code
        return client.get('/api/bridges').status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        codes = list(pool.map(read, range(24)))
    assert codes == [200] * 24
    assert world['monitor'].data_version() == baseline
