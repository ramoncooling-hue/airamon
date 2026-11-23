from contextlib import closing

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes
from app.memory import db, repo


@pytest.fixture
def scoped_data(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'APP_DIR', tmp_path / 'app')
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'app' / 'test.db')
    monkeypatch.setattr(db, 'DATA_DIR', tmp_path / 'data')
    monkeypatch.setattr(repo, 'DATA_DIR', tmp_path / 'data')
    monkeypatch.setattr(routes, 'STUDYPAL_DIR', tmp_path / 'data' / 'StudyPal')
    db.apply_schema()
    courses = {
        key: repo.create_course(f'{key}-course', workspace=key)
        for key in ('studypal', 'workpal', 'lifepal')
    }
    repo.create_workspace('empty', 'Empty workspace')
    courses['empty'] = repo.create_course('empty-course', workspace='empty')
    nodes = {}
    with closing(db.connect()) as conn:
        for key, course in courses.items():
            if key == 'empty':
                continue
            nodes[key] = [conn.execute(
                '''INSERT INTO kp(course_id,name,chapter,gist,band,mastery,freq,kp_type)
                   VALUES(?,?,?,?,?,?,?,?)''',
                (course['id'], f'{key}-{index}', 'Topic', f'{key} content',
                 'A', '未读', '高频', 'concept'),
            ).lastrowid for index in range(2)]
            conn.execute('INSERT INTO kp_edges(from_kp,to_kp,kind) VALUES(?,?,?)',
                         (*nodes[key], 'supports'))
            conn.execute('INSERT INTO source_files(course_id,path,status) VALUES(?,?,?)',
                         (course['id'], str(tmp_path / key / 'material.md'), 'indexed'))
            conn.execute('INSERT INTO notices(record_id,course_id,course_name,title) VALUES(?,?,?,?)',
                         (f'{key}-notice', course['id'], course['name'], f'{key} notice'))
        conn.execute('INSERT INTO kp_edges(from_kp,to_kp,kind) VALUES(?,?,?)',
                     (nodes['studypal'][0], nodes['workpal'][0], 'related'))
        conn.commit()
    for key in nodes:
        repo.create_event({'title': f'{key} event', 'date': '2026-10-16',
                           'course_name': courses[key]['name']})
        repo.create_workspace_item(key, {'kind': 'task', 'title': f'{key} task',
                                        'due_at': '2026-10-17'})
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as client:
        yield client, courses, nodes


@pytest.mark.parametrize('workspace', ['studypal', 'workpal', 'lifepal', 'empty', 'missing', ''])
def test_kp_and_course_lists_are_scoped(scoped_data, workspace):
    client, courses, nodes = scoped_data
    expected = set(nodes.get(workspace, []))
    response = client.get('/api/kp', params={'workspace': workspace})
    assert response.status_code == 200
    assert {row['id'] for row in response.json()} == expected
    assert {row['id'] for row in repo.list_kp(workspace=workspace)} == expected
    response = client.get('/api/courses', params={'workspace': workspace})
    assert response.status_code == 200
    assert {row['name'] for row in response.json()} == (
        {courses[workspace]['name']} if workspace in courses else set()
    )


def test_kp_filters_intersect_with_workspace_and_preserve_positional_calls(scoped_data):
    client, courses, nodes = scoped_data
    study = courses['studypal']['name']
    expected = set(nodes['studypal'])
    assert {row['id'] for row in repo.list_kp(study, 'A', '未读', '高频')} == expected
    assert client.get('/api/kp', params={'course': study, 'workspace': 'workpal'}).json() == []
    assert client.get('/api/kp', params={
        'course': study, 'workspace': 'studypal', 'band': 'A', 'mastery': '未读', 'freq': '高频',
    }).json() == repo.list_kp(study)
    assert client.get('/api/kp', params={'workspace': 'studypal', 'band': 'absent'}).json() == []


def test_omitted_workspace_preserves_legacy_list_reads(scoped_data):
    client, courses, nodes = scoped_data
    assert {row['id'] for row in client.get('/api/kp').json()} == {
        node_id for workspace_nodes in nodes.values() for node_id in workspace_nodes
    }
    assert {row['name'] for row in client.get('/api/courses').json()} == {
        course['name'] for course in courses.values()
    }
    assert len(client.get('/api/events').json()) == 6
    assert len(client.get('/api/notices').json()) == 3


@pytest.mark.parametrize('workspace', ['studypal', 'workpal', 'lifepal'])
def test_graph_only_contains_selected_course_nodes_and_edges(scoped_data, workspace):
    client, courses, nodes = scoped_data
    course = courses[workspace]['name']
    response = client.get('/api/graph', params={'course': course, 'workspace': workspace})
    assert response.status_code == 200
    graph = response.json()
    assert {node['id'] for node in graph['nodes']} == set(nodes[workspace])
    assert len(graph['edges']) == 1
    assert all(edge['from_kp'] in nodes[workspace] and edge['to_kp'] in nodes[workspace]
               for edge in graph['edges'])
    assert {node['id'] for node in repo.graph_of(course)['nodes']} == set(nodes[workspace])
    assert client.get('/api/graph', params={'course': course}).status_code == 200


@pytest.mark.parametrize('path', ['/api/graph', '/api/mastery', '/api/sources', '/api/knowledge/profile'])
@pytest.mark.parametrize('workspace', ['workpal', 'empty', 'missing', ''])
def test_course_reads_reject_a_course_outside_workspace(scoped_data, path, workspace):
    client, courses, _ = scoped_data
    response = client.get(path, params={'course': courses['studypal']['name'], 'workspace': workspace})
    assert response.status_code == 404


def test_empty_course_does_not_fall_back_to_study(scoped_data):
    client, courses, _ = scoped_data
    params = {'course': courses['empty']['name'], 'workspace': 'empty'}
    graph = client.get('/api/graph', params=params)
    assert graph.status_code == 200
    assert graph.json()['nodes'] == []
    assert graph.json()['edges'] == []
    assert client.get('/api/mastery', params=params).json()['rows'] == []
    materials = client.get('/api/sources', params=params)
    assert materials.status_code == 200
    assert materials.json()['files'] == []
    assert materials.json()['materials'] == []
    assert materials.json()['candidates'] == []


@pytest.mark.parametrize('workspace', ['studypal', 'workpal', 'lifepal'])
def test_mastery_and_sources_keep_course_only_compatibility(scoped_data, workspace):
    client, courses, _ = scoped_data
    course = courses[workspace]['name']
    for path in ('/api/mastery', '/api/sources', '/api/knowledge/profile'):
        legacy = client.get(path, params={'course': course})
        scoped = client.get(path, params={'course': course, 'workspace': workspace})
        assert legacy.status_code == scoped.status_code == 200
        if path != '/api/knowledge/profile':
            assert legacy.json() == scoped.json()
    assert repo.mastery_matrix(course)['rows'][0]['total'] == 2
    assert len(client.get('/api/sources', params={'course': course}).json()['files']) == 1


@pytest.mark.parametrize('workspace', ['studypal', 'workpal', 'lifepal', 'empty', 'missing', ''])
def test_events_notices_and_task_projections_are_scoped(scoped_data, workspace):
    client, courses, nodes = scoped_data
    events = client.get('/api/events', params={'workspace': workspace})
    assert events.status_code == 200
    assert {row['title'] for row in events.json()} == (
        {f'{workspace} event', f'{workspace} task'} if workspace in nodes else set()
    )
    assert all(row['workspace_key'] == workspace for row in events.json())
    notices = client.get('/api/notices', params={'workspace': workspace})
    assert {row['title'] for row in notices.json()} == (
        {f'{workspace} notice'} if workspace in nodes else set()
    )
    assert client.get('/api/events', params={
        'workspace': workspace, 'course': courses['studypal']['name'],
    }).json() == (repo.list_events(courses['studypal']['name']) if workspace == 'studypal' else [])


@pytest.mark.parametrize('course_field', ['course_id', 'course_name'])
def test_event_create_rejects_course_from_another_workspace(scoped_data, course_field):
    client, courses, _ = scoped_data
    study = courses['studypal']
    body = {'title': 'Invalid event', 'date': '2026-10-18', 'workspace_key': 'workpal',
            course_field: study['id' if course_field == 'course_id' else 'name']}
    response = client.post('/api/events', json=body)
    assert response.status_code == 400
    assert all(row['title'] != 'Invalid event' for row in repo.list_events())


def test_event_create_rejects_inconsistent_course_id_and_name(scoped_data):
    client, courses, _ = scoped_data
    response = client.post('/api/events', json={
        'title': 'Invalid event', 'date': '2026-10-18', 'workspace_key': 'workpal',
        'course_id': courses['workpal']['id'], 'course_name': courses['studypal']['name'],
    })
    assert response.status_code == 400


@pytest.mark.parametrize('field', ['workspace_id', 'course_name'])
def test_event_update_rejects_cross_workspace_course_binding(scoped_data, field):
    client, courses, _ = scoped_data
    event = repo.list_events(courses['workpal']['name'])[0]
    value = courses['studypal']['workspace_id' if field == 'workspace_id' else 'name']
    response = client.patch(f"/api/events/{event['id']}", json={field: value})
    assert response.status_code == 400
    assert repo.list_events(courses['workpal']['name'])[0] == event
    with closing(db.connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM event_revisions WHERE event_id=?',
                            (event['id'],)).fetchone()[0] == 1


def test_existing_inconsistent_event_does_not_leak_course_data(scoped_data):
    client, courses, _ = scoped_data
    study, work = courses['studypal'], courses['workpal']
    with closing(db.connect()) as conn:
        conn.execute('''INSERT INTO events(course_id,course_name,workspace_id,title,date)
                        VALUES(?,?,?,?,?)''',
                     (study['id'], study['name'], work['workspace_id'], 'Invalid legacy event', '2026-10-18'))
        conn.commit()
    response = client.get('/api/events', params={'workspace': 'workpal', 'course': study['name']})
    assert response.json() == []
    assert {row['title'] for row in client.get('/api/events', params={'workspace': 'workpal'}).json()} == {
        'workpal event', 'workpal task',
    }


@pytest.mark.parametrize('course_field', ['course_id', 'course_name'])
def test_valid_event_write_normalizes_course_and_keeps_revision(scoped_data, course_field):
    client, courses, _ = scoped_data
    work = courses['workpal']
    response = client.post('/api/events', json={
        'title': 'New event', 'date': '2026-10-18', 'workspace_key': 'workpal',
        course_field: work['id' if course_field == 'course_id' else 'name'],
    })
    assert response.status_code == 200
    event = response.json()
    assert (event['course_id'], event['course_name'], event['workspace_id']) == (
        work['id'], work['name'], work['workspace_id'],
    )
    response = client.patch(f"/api/events/{event['id']}", json={'title': 'Updated event'})
    assert response.status_code == 200
    assert response.json()['title'] == 'Updated event'
    with closing(db.connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM event_revisions WHERE event_id=?',
                            (event['id'],)).fetchone()[0] == 2


@pytest.mark.parametrize('course_field', ['course_id', 'course_name'])
def test_event_can_change_course_inside_workspace(scoped_data, course_field):
    client, courses, _ = scoped_data
    other = repo.create_course('another-work-course', workspace='workpal')
    event = repo.list_events(courses['workpal']['name'])[0]
    response = client.patch(f"/api/events/{event['id']}", json={
        course_field: other['id' if course_field == 'course_id' else 'name'],
    })
    assert response.status_code == 200
    assert response.json()['course_id'] == other['id']
    assert response.json()['course_name'] == other['name']
    assert response.json()['workspace_id'] == other['workspace_id']


def test_event_without_explicit_workspace_keeps_course_inference(scoped_data):
    client, courses, _ = scoped_data
    work = courses['workpal']
    response = client.post('/api/events', json={
        'title': 'Course-only event', 'date': '2026-10-18', 'course_name': work['name'],
    })
    assert response.status_code == 200
    assert response.json()['workspace_id'] == work['workspace_id']


@pytest.mark.parametrize('workspace', ['', 'missing'])
def test_event_explicit_invalid_workspace_does_not_fall_back_to_study(scoped_data, workspace):
    client, _, _ = scoped_data
    response = client.post('/api/events', json={
        'title': 'Invalid workspace', 'date': '2026-10-18', 'workspace_key': workspace,
    })
    assert response.status_code == 400
    assert all(row['title'] != 'Invalid workspace' for row in repo.list_events())


def test_legacy_null_workspace_records_belong_only_to_study(scoped_data):
    client, courses, nodes = scoped_data
    with closing(db.connect()) as conn:
        conn.execute('UPDATE courses SET workspace_id=NULL WHERE id=?', (courses['studypal']['id'],))
        conn.execute('UPDATE events SET workspace_id=NULL WHERE course_name=?',
                     (courses['studypal']['name'],))
        conn.commit()
    assert {row['id'] for row in client.get('/api/kp', params={'workspace': 'studypal'}).json()} == set(nodes['studypal'])
    assert client.get('/api/graph', params={
        'course': courses['studypal']['name'], 'workspace': 'studypal',
    }).status_code == 200
    assert all(row['workspace_key'] == 'workpal'
               for row in client.get('/api/events', params={'workspace': 'workpal'}).json())
