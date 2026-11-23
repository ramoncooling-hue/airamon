"""B3 T3.9 复习队列 v0：固定规则、三档反馈、考试优先（纯查询，不新增表）。"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes
from app.memory import db, repo, review


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / 'data'
    db_path = root / 'app' / 'banxue.db'
    monkeypatch.setattr(db, 'DATA_DIR', root)
    monkeypatch.setattr(db, 'APP_DIR', root / 'app')
    monkeypatch.setattr(db, 'DB_PATH', db_path)
    db.apply_schema()
    conn = db.connect()
    try:
        course_id = conn.execute("INSERT INTO courses(name) VALUES('复习课')").lastrowid
        other_id = conn.execute("INSERT INTO courses(name) VALUES('另一门课')").lastrowid
        ids = {}
        for name, mastery, freq in (
            ('从没读过', '未读', '高频'),
            ('三天前读过', '读过', '普通'),
            ('刚刚复习过', '会用', '普通'),
            ('熟练很久', '熟练', '普通'),
            ('熟练刚复习', '熟练', '普通'),
        ):
            ids[name] = conn.execute(
                "INSERT INTO kp(course_id,name,chapter,mastery,freq,gist) VALUES(?,?,?,?,?,?)",
                (course_id, name, '第一章', mastery, freq, '要点')).lastrowid
        ids['别的课'] = conn.execute(
            "INSERT INTO kp(course_id,name,chapter,mastery,freq,gist) VALUES(?,?,?,?,?,?)",
            (other_id, '别的课', '第一章', '未读', '普通', '要点')).lastrowid
        conn.execute(
            """INSERT INTO events(course_id,course_name,title,category,date)
               VALUES(?,?,?,?,date('now','localtime','+3 day'))""",
            (course_id, '复习课', '复习课小测', 'exam'))
        conn.commit()
    finally:
        conn.close()
    return {'root': root, 'course': '复习课', 'ids': ids,
            'course_id': course_id, 'other_id': other_id}


@pytest.fixture
def client(world, monkeypatch):
    app = FastAPI()
    app.include_router(routes.router)
    monkeypatch.setattr(routes.workflow_service, 'recover_stale_runs', lambda: 0,
                        raising=False)
    return TestClient(app)


def _age(conn, kp_id, days):
    conn.execute(
        "UPDATE kp SET updated_at = datetime('now','localtime',?) WHERE id = ?",
        (f'-{days} day', kp_id))


def test_unread_is_top_priority_and_filtered(client, world):
    data = client.get('/api/review/due').json()
    names = [item['name'] for item in data['items']]
    assert names[0] == '从没读过'
    assert data['items'][0]['reason'] == '还没开始读'
    assert data['total'] == len(names)


def test_course_filter_narrows_queue(client):
    data = client.get('/api/review/due', params={'course': '另一门课'}).json()
    assert [item['name'] for item in data['items']] == ['别的课']


def test_just_reviewed_is_skipped(client, world):
    conn = db.connect()
    try:
        kp_id = world['ids']['刚刚复习过']
        repo.write_mastery(kp_id, '会用')
        conn.execute("UPDATE kp SET updated_at = datetime('now','localtime') WHERE id = ?",
                     (kp_id,))
        conn.commit()
        first = review.feedback(kp_id, 'hard')
        assert first['ok'] is True
        second = review.feedback(kp_id, 'good')
        assert second['mastery'] == '会用'
    finally:
        conn.close()
    data = client.get('/api/review/due').json()
    assert '刚刚复习过' not in [item['name'] for item in data['items']]


def test_mastery_gap_rules(world):
    conn = db.connect()
    try:
        _age(conn, world['ids']['三天前读过'], 4)
        _age(conn, world['ids']['熟练很久'], 20)
        _age(conn, world['ids']['熟练刚复习'], 2)
        conn.commit()
    finally:
        conn.close()
    data = review.due(course='复习课')
    names = [item['name'] for item in data['items']]
    assert '三天前读过' in names
    assert '熟练很久' in names
    assert '熟练刚复习' not in names


def test_exam_soon_boosts_high_frequency(client):
    data = client.get('/api/review/due').json()
    assert data['exam_dates']
    exam = data['exam_dates'][0]
    assert exam['course_name'] == '复习课'
    assert exam['days_left'] <= review.EXAM_SOON_DAYS
    unread = next(item for item in data['items'] if item['name'] == '从没读过')
    assert unread['reason'] == '还没开始读'
    assert unread['priority'] >= 100 + review.EXAM_HIGH_FREQ_BONUS


def test_feedback_downgrades_mastery_and_records_event(world):
    result = review.feedback(world['ids']['熟练很久'], 'again')
    assert result['ok'] is True
    assert result['mastery'] == '读过'
    kp = repo.get_kp(world['ids']['熟练很久'])
    assert kp['mastery'] == '读过'
    conn = db.connect()
    try:
        revisions = conn.execute(
            "SELECT field, old_value, new_value, origin FROM revisions WHERE kp_id = ?",
            (world['ids']['熟练很久'],)).fetchall()
    finally:
        conn.close()
    assert revisions
    assert dict(revisions[-1]) == {'field': 'mastery', 'old_value': '熟练',
                                   'new_value': '读过', 'origin': 'review'}


def test_feedback_hard_and_good_paths(world):
    hard = review.feedback(world['ids']['从没读过'], 'hard')
    assert hard['mastery'] == '会用'
    good = review.feedback(world['ids']['熟练很久'], 'good')
    assert good['mastery'] == '熟练'
    assert good['changed'] is False


def test_feedback_rejects_bad_input(world):
    with pytest.raises(review.ReviewError):
        review.feedback(world['ids']['从没读过'], 'perfect')
    with pytest.raises(review.ReviewError):
        review.feedback(999999, 'good')


def test_endpoints_validate(client, world):
    assert client.post('/api/review/feedback',
                       json={'kp_id': world['ids']['从没读过'],
                             'result': 'nope'}).status_code == 422
    missing = client.post('/api/review/feedback',
                          json={'kp_id': 987654, 'result': 'good'})
    assert missing.status_code == 404
    assert client.get('/api/review/due', params={'limit': 'abc'}).status_code == 422


def test_due_is_read_only(client, world):
    import sqlite3
    monitor = sqlite3.connect(str(db.DB_PATH))
    try:
        before = monitor.execute('PRAGMA data_version').fetchone()[0]
        client.get('/api/review/due')
        after = monitor.execute('PRAGMA data_version').fetchone()[0]
    finally:
        monitor.close()
    assert before == after


def test_soft_deleted_kp_never_appears(world):
    conn = db.connect()
    try:
        conn.execute("UPDATE kp SET deleted_at = datetime('now','localtime') WHERE id = ?",
                     (world['ids']['从没读过'],))
        conn.commit()
    finally:
        conn.close()
    names = [item['name'] for item in review.due(course='复习课')['items']]
    assert '从没读过' not in names


def _add_exam(conn, course_id, course_name, days):
    conn.execute(
        """INSERT INTO events(course_id,course_name,title,category,date)
           VALUES(?,?,?,?,date('now','localtime',?))""",
        (course_id, course_name, course_name + '考试', 'exam', f'+{int(days)} day'))


def test_exam_dates_are_workspace_scoped(world):
    """P2-3：考试事件按课程所属工作区过滤，别的工作区的考试不参与本工作区。"""
    conn = db.connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO workspaces(key, name, status) VALUES('workpal', '工作', 'active')")
        workpal_id = conn.execute(
            "SELECT id FROM workspaces WHERE key = 'workpal'").fetchone()[0]
        workpal_course = conn.execute(
            "INSERT INTO courses(name, workspace_id) VALUES('工作课', ?)", (workpal_id,)
        ).lastrowid
        _add_exam(conn, workpal_course, '工作课', 2)
        conn.commit()
    finally:
        conn.close()
    studypal = review.due(workspace='studypal')
    assert [(row['course_name'], row['days_left']) for row in studypal['exam_dates']] == [
        ('复习课', 3)]
    unread = next(item for item in studypal['items'] if item['name'] == '从没读过')
    assert unread['priority'] == 100 + review.EXAM_HIGH_FREQ_BONUS + 5
    workpal = review.due(workspace='workpal')
    assert [(row['course_name'], row['days_left']) for row in workpal['exam_dates']] == [
        ('工作课', 2)]
    assert workpal['items'] == []


def test_exam_event_workspace_column_wins(world):
    """P2-3 残余：events.workspace_id 有值时以它为准，不靠课程归属回退。"""
    conn = db.connect()
    try:
        workpal_id = conn.execute(
            "SELECT id FROM workspaces WHERE key = 'workpal'").fetchone()[0]
        conn.execute(
            "INSERT INTO events(course_id, course_name, title, category, date, workspace_id)"
            " VALUES(NULL, '外部考试', '外部考试', 'exam', date('now','localtime','+1 day'), ?)",
            (workpal_id,))
        conn.commit()
    finally:
        conn.close()
    studypal = [row['course_name'] for row in review.due(workspace='studypal')['exam_dates']]
    assert '外部考试' not in studypal
    workpal = [row['course_name'] for row in review.due(workspace='workpal')['exam_dates']]
    assert '外部考试' in workpal


def test_same_course_uses_nearest_exam(world):
    """P2-3：同一课程多场考试只取最近一场，远期考试不覆盖近期考试。"""
    conn = db.connect()
    try:
        _add_exam(conn, world['course_id'], '复习课', 20)
        conn.execute(
            "UPDATE kp SET freq = '高频', updated_at = datetime('now','localtime','-4 day') "
            "WHERE id = ?", (world['ids']['三天前读过'],))
        conn.commit()
    finally:
        conn.close()
    data = review.due(course='复习课')
    assert [(row['course_name'], row['days_left']) for row in data['exam_dates']] == [
        ('复习课', 3)]
    item = next(item for item in data['items'] if item['name'] == '三天前读过')
    assert item['reason'] == '3 天后考试'
    assert item['priority'] == 70 + review.EXAM_HIGH_FREQ_BONUS + 5
