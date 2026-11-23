"""B3 T3.10: tag system backend (ensure/list/attach/detach/filter, O-KB-5)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes
from app.memory import db, documents, repo


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / 'data'
    monkeypatch.setattr(db, 'DATA_DIR', root)
    monkeypatch.setattr(db, 'APP_DIR', root / 'app')
    monkeypatch.setattr(db, 'DB_PATH', root / 'app' / 'banxue.db')
    db.apply_schema()
    conn = db.connect()
    try:
        course_id = conn.execute("INSERT INTO courses(name) VALUES('会计学')").lastrowid
        conn.execute(
            "UPDATE courses SET workspace_id=(SELECT id FROM workspaces WHERE key='studypal') "
            "WHERE id=?", (course_id,))
        kp_id = conn.execute(
            "INSERT INTO kp(course_id,name,gist) VALUES(?,?,?)",
            (course_id, '边际成本', '新增一单位产量增加的成本。')).lastrowid
        other_kp = conn.execute(
            "INSERT INTO kp(course_id,name,gist) VALUES(?,?,?)",
            (course_id, '固定成本', '不随产量变化的成本。')).lastrowid
        conn.commit()
    finally:
        conn.close()
    return {'course_id': course_id, 'course': '会计学',
            'kp_id': kp_id, 'other_kp': other_kp}


@pytest.fixture
def client(world, monkeypatch):
    app = FastAPI()
    app.include_router(routes.router)
    monkeypatch.setattr(routes.workflow_service, 'recover_stale_runs', lambda: 0,
                        raising=False)
    return TestClient(app)


def _count(table, where='1=1', params=()):
    conn = db.connect()
    try:
        return conn.execute(f'SELECT COUNT(*) FROM {table} WHERE {where}', params).fetchone()[0]
    finally:
        conn.close()


def _document(client, title='边际成本笔记', tags=None, body_md='新增产量的成本。'):
    payload = {'title': title, 'body_md': body_md}
    if tags is not None:
        payload['tags'] = tags
    response = client.post('/api/documents', json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _tag_id(client, name, workspace='studypal'):
    response = client.post('/api/tags', json={'workspace': workspace, 'name': name})
    assert response.status_code == 200, response.text
    return response.json()['tag']['id']


def test_create_tag_is_idempotent(client):
    first = client.post('/api/tags', json={'name': ' 经济学 '})
    assert first.status_code == 200
    tag = first.json()['tag']
    assert tag['name'] == '经济学'
    assert tag['created'] is True
    second = client.post('/api/tags', json={'workspace': 'studypal', 'name': '经济学'})
    again = second.json()['tag']
    assert again['id'] == tag['id']
    assert again['created'] is False
    assert _count('tags', 'name=?', ('经济学',)) == 1
    assert client.post('/api/tags', json={'name': '   '}).status_code == 400
    assert client.post('/api/tags', json={'workspace': 'nope', 'name': 'x'}).status_code == 404


def test_list_tags_counts_documents_and_kps(client, world):
    doc = _document(client, tags=['经济学'])
    tag_id = _tag_id(client, '经济学') if not _document else None
    # the document already created the tag; re-create to get its id
    tag = next(item for item in client.get('/api/tags').json() if item['name'] == '经济学')
    tag_id = tag['id']
    attach = client.post('/api/entity-tags', json={
        'tag': '经济学', 'entity_type': 'kp', 'entity_id': world['kp_id']})
    assert attach.status_code == 200
    listed = {item['name']: item for item in client.get('/api/tags').json()}
    tag = listed['经济学']
    assert tag['count'] == 2
    assert tag['document_count'] == 1
    assert tag['entity_types'] == {'document': 1, 'kp': 1, 'course': 0}
    assert isinstance(tag['id'], int) and tag['id'] == tag_id
    assert [item['name'] for item in client.get('/api/tags', params={'workspace': 'studypal'}).json()]         == ['经济学']
    assert doc['id'] == client.get('/api/documents', params={'tag': '经济学'}).json()[0]['id']


def test_documents_filter_by_tag_and_query(client):
    one = _document(client, title='边际成本笔记', tags=['经济学'])
    _document(client, title='固定资产折旧', tags=['会计'])
    filtered = client.get('/api/documents', params={'tag': '经济学'}).json()
    assert [item['id'] for item in filtered] == [one['id']]
    assert client.get('/api/documents', params={'tag': '经济学', 'q': '边际成本'}).json()
    assert client.get('/api/documents', params={'tag': '经济学', 'q': '折旧'}).json() == []
    assert client.get('/api/documents', params={'tag': '不存在的标签'}).status_code == 404


def test_delete_tag_cascades_entity_tags(client):
    doc = _document(client, tags=['经济学'])
    tag_id = next(item['id'] for item in client.get('/api/tags').json()
                  if item['name'] == '经济学')
    assert _count('entity_tags', 'tag_id=?', (tag_id,)) == 1
    response = client.delete(f'/api/tags/{tag_id}')
    assert response.status_code == 200
    assert response.json()['deleted']['removed'] is True
    assert _count('tags', 'id=?', (tag_id,)) == 0
    assert _count('entity_tags', 'tag_id=?', (tag_id,)) == 0
    assert client.get('/api/tags').json() == []
    assert client.get('/api/documents', params={'tag': '经济学'}).status_code == 404
    assert client.delete('/api/tags/9999').status_code == 404
    assert doc['id']


def test_attach_rejects_soft_deleted_kp(client, world):
    _document(client, tags=['经济学'])
    repo.soft_delete_kp(world['kp_id'])
    response = client.post('/api/entity-tags', json={
        'tag': '经济学', 'entity_type': 'kp', 'entity_id': world['kp_id']})
    assert response.status_code == 404
    assert '已删除' in response.json()['detail']


def test_unknown_tag_and_entity_are_404(client, world):
    doc = _document(client)
    response = client.post('/api/entity-tags', json={
        'tag': '不存在', 'entity_type': 'document', 'entity_id': doc['id']})
    assert response.status_code == 404
    assert client.post('/api/entity-tags', json={
        'tag': '经济学', 'entity_type': 'document', 'entity_id': 9999}).status_code == 404
    assert client.post('/api/entity-tags', json={
        'tag': '经济学', 'entity_type': 'kp', 'entity_id': world['kp_id']}).status_code == 404
    assert client.post('/api/entity-tags', json={
        'tag': '经济学', 'entity_type': 'bad', 'entity_id': 1}).status_code == 422
    assert client.get('/api/tags/9999/entities').status_code == 404


def test_entities_of_tag_returns_titles_and_counts(client, world):
    doc = _document(client, title='边际成本笔记', tags=['经济学'])
    _tag_id(client, '经济学')
    assert client.post('/api/entity-tags', json={
        'tag': '经济学', 'entity_type': 'kp', 'entity_id': world['kp_id']}).status_code == 200
    tag_id = next(item['id'] for item in client.get('/api/tags').json()
                  if item['name'] == '经济学')
    data = client.get(f'/api/tags/{tag_id}/entities').json()
    assert data['tag']['name'] == '经济学'
    titles = {(item['entity_type'], item['title']) for item in data['entities']}
    assert ('document', '边际成本笔记') in titles
    assert ('kp', '边际成本') in titles
    assert data['counts'] == {'document': 1, 'kp': 1, 'course': 0, 'total': 2}
    assert doc['id']


def test_list_tags_ordering_is_stable(client):
    for name in ('zzz', 'aaa', 'mmm'):
        _tag_id(client, name)
    names = [item['name'] for item in client.get('/api/tags').json()]
    assert names == ['aaa', 'mmm', 'zzz']


def test_detach_entity_removes_link(client):
    doc = _document(client, tags=['经济学'])
    params = {'tag': '经济学', 'entity_type': 'document', 'entity_id': doc['id']}
    first = client.delete('/api/entity-tags', params=params)
    assert first.status_code == 200
    assert first.json()['link']['removed'] is True
    second = client.delete('/api/entity-tags', params=params)
    assert second.json()['link']['removed'] is False
    tag = next(item for item in client.get('/api/tags').json() if item['name'] == '经济学')
    assert tag['count'] == 0
    assert client.delete('/api/entity-tags', params={
        'tag': '不存在', 'entity_type': 'document', 'entity_id': doc['id']}).status_code == 404


def test_search_tag_filter_keeps_only_tagged_kp(client, world):
    _document(client, tags=['经济学'])
    assert client.post('/api/entity-tags', json={
        'tag': '经济学', 'entity_type': 'kp', 'entity_id': world['kp_id']}).status_code == 200
    tagged = client.get('/api/search', params={'q': '成本', 'tag': '经济学'}).json()
    assert tagged['hits'], tagged
    assert {hit['kind'] for hit in tagged['hits']} == {'kp'}
    assert [str(hit['ref_id']) for hit in tagged['hits']] == [str(world['kp_id'])]
    everything = client.get('/api/search', params={'q': '成本'}).json()
    refs = {str(hit['ref_id']) for hit in everything['hits'] if hit['kind'] == 'kp'}
    assert {str(world['kp_id']), str(world['other_kp'])} <= refs
    assert client.get('/api/search', params={'q': '成本', 'tag': '不存在'}).status_code == 404


def test_attach_and_detach_course_entity(client, world):
    tag_id = _tag_id(client, '会计')
    attached = client.post('/api/entity-tags', json={
        'tag': '会计', 'entity_type': 'course', 'entity_id': world['course_id']})
    assert attached.status_code == 200
    entities = client.get(f'/api/tags/{tag_id}/entities').json()
    assert entities['counts']['course'] == 1
    assert entities['entities'][0]['title'] == '会计学'
    removed = client.delete('/api/entity-tags', params={
        'tag': '会计', 'entity_type': 'course', 'entity_id': world['course_id']})
    assert removed.status_code == 200
    assert removed.json()['link']['removed'] is True
