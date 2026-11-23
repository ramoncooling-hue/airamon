"""Local-only mapping assistant HTTP contract; no lifespan, real settings, or network."""

import json
from contextlib import closing

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent import llm
from app.api import ontology_routes
from app.memory import db


ROOT = '/api/knowledge/ontology/mappings'
BASE = 'http://localhost:8765'
PEER = ('127.0.0.1', 43210)
PRIVATE = '/private/fixture/provider-secret'


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'APP_DIR', tmp_path)
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'assistant-api.db')
    db.apply_schema()
    with closing(db.connect()) as conn, conn:
        owner = conn.execute("SELECT id FROM workspaces WHERE key='workpal'").fetchone()[0]
        course = conn.execute("INSERT INTO courses(name,workspace_id) VALUES('Material',?)", (owner,)).lastrowid
        source = conn.execute('''INSERT INTO source_files(course_id,path,sha256,status,source_kind)
                                 VALUES(?,?,?,'indexed','wiki_page')''', (course, PRIVATE + '/material.md', 'a' * 64)).lastrowid
        snapshot = conn.execute('INSERT INTO source_snapshots(source_id,sha256) VALUES(?,?)', (source, 'a' * 64)).lastrowid
        conn.execute('''INSERT INTO evidence(source_id,snapshot_id,locator_type,locator_start,locator_end,content)
                        VALUES(?,?,'line',1,2,'Synthetic evidence')''', (source, snapshot))
    def chat(*a, **k):
        return json.dumps({'branch_keys': ['work.industry_knowledge'], 'node_types': ['method'],
                           'relation_kinds': ['supports'], 'field_mappings': {'title': 'name'}})
    def forbidden(*a, **k):
        raise AssertionError('No personal settings or real remote calls')
    monkeypatch.setattr(llm, 'chat', chat)
    monkeypatch.setattr(llm, '_model_config', forbidden)
    app = FastAPI()
    app.include_router(ontology_routes.router, prefix='/api')
    with TestClient(app, base_url=BASE, client=PEER) as value:
        yield value


def body(**changes):
    return {'workspace_key': 'workpal', 'source_id': 1, 'profile_id': 'work', 'purpose': '整理资料', **changes}


def test_inputs_samples_suggest_evidence_and_existing_human_review(client):
    inputs = client.get(ROOT + '/inputs', params={'workspace_key': 'workpal'})
    assert inputs.status_code == 200, inputs.text
    assert inputs.json()['sources'] == [{'id': 1, 'name': 'Material · material.md', 'evidence_count': 1}]
    samples = client.get(ROOT + '/samples', params={'workspace_key': 'workpal', 'source_id': 1})
    assert samples.status_code == 200
    assert samples.json()['selected'] == samples.json()['total'] == 1
    result = client.post(ROOT + '/suggest', json=body())
    assert result.status_code == 200, result.text
    row = result.json()
    assert row['status'] == 'draft'
    bound = client.get(f"{ROOT}/{row['id']}/evidence", params={'workspace_key': 'workpal'})
    assert bound.status_code == 200
    assert bound.json() == samples.json()
    assert client.get(f"{ROOT}/{row['id']}", params={'workspace_key': 'workpal'}).json() == row
    assert all(PRIVATE not in response.text for response in (inputs, samples, result, bound))
    command = {'workspace_key': 'workpal', 'expected_hash': row['mapping_hash']}
    assert client.post(f"{ROOT}/{row['id']}/submit", json=command).status_code == 200
    assert client.post(f"{ROOT}/{row['id']}/approve", json=command).status_code == 422
    approved = client.post(f"{ROOT}/{row['id']}/approve", json={**command, 'confirmed': True})
    assert approved.status_code == 200
    assert approved.json()['status'] == 'approved'
    with closing(db.connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM kp').fetchone()[0] == 0


@pytest.mark.parametrize('change', [
    {'source_id': True}, {'source_id': 1.0}, {'source_id': '1'}, {'source_id': None},
    {'source_id': 0}, {'source_id': 2**63}, {'purpose': ''}, {'purpose': ' '},
    {'purpose': 100}, {'purpose': 'x' * 2001}, {'profile_id': True},
    {'workspace_key': 10}, {'workspace_key': ''}, {'source_id': []},
    {'status': 'approved'}, {'model_id': PRIVATE}, {'sample_refs': ['evidence:999']},
    {'base_version_id': 1}, {'created_by': PRIVATE},
])
def test_strict_body_rejects_invalid_or_host_owned_fields(client, change):
    result = client.post(ROOT + '/suggest', json=body(**change))
    assert result.status_code == 422, result.text
    assert PRIVATE not in result.text


@pytest.mark.parametrize('raw', ['{', '[]', 'null', '{"purpose":NaN}',
                               '{"workspace_key":"workpal","source_id":1,"source_id":2,"profile_id":"work","purpose":"x"}'])
def test_malformed_and_duplicate_request_json_is_sanitized(client, raw):
    result = client.post(ROOT + '/suggest', content=raw, headers={'content-type': 'application/json'})
    assert result.status_code == 422, result.text


@pytest.mark.parametrize('suffix', ['/inputs', '/samples', '/1/evidence'])
def test_query_scope_required(client, suffix):
    assert client.get(ROOT + suffix).status_code == 422


@pytest.mark.parametrize('source_id', ['0', '-1', 'true', '1.5', '9223372036854775808'])
def test_sample_source_query_is_a_bounded_integer(client, source_id):
    response = client.get(ROOT + '/samples', params={'workspace_key': 'workpal', 'source_id': source_id})
    assert response.status_code == 422


@pytest.mark.parametrize('method,suffix', [('get', '/inputs'), ('get', '/samples'),
                                         ('get', '/1/evidence'), ('post', '/suggest')])
@pytest.mark.parametrize('access', ['remote', 'remote-host', 'cross-origin', 'cross-site'])
def test_every_assistant_endpoint_enforces_local_same_origin(client, method, suffix, access):
    app = FastAPI()
    app.include_router(ontology_routes.router, prefix='/api')
    base = 'http://192.0.2.1:8765' if access == 'remote-host' else BASE
    peer = ('192.0.2.2', 1234) if access == 'remote' else PEER
    headers = {'origin': 'https://evil.example'} if access == 'cross-origin' else {}
    if access == 'cross-site':
        headers['sec-fetch-site'] = 'cross-site'
    with TestClient(app, base_url=base, client=peer) as blocked:
        kwargs = {'json': body()} if method == 'post' else {'params': {'workspace_key': 'workpal', 'source_id': 1}}
        result = getattr(blocked, method)(ROOT + suffix, headers=headers, **kwargs)
    assert result.status_code == 403, result.text


@pytest.mark.parametrize('reason,status', [('model', 502), ('invalid', 422), ('conflict', 409)])
def test_failure_statuses_are_chinese_sanitized_and_leave_no_draft(client, monkeypatch, reason, status):
    def fail(*a, **k):
        if reason == 'model':
            raise llm.LLMError(PRIVATE + ' API_KEY')
        if reason == 'conflict':
            with closing(db.connect()) as conn, conn:
                conn.execute("UPDATE evidence SET content='New claim'")
        return '{}' if reason == 'invalid' else json.dumps({
            'branch_keys': ['work.industry_knowledge'], 'node_types': ['method'],
            'relation_kinds': [], 'field_mappings': {},
        })
    monkeypatch.setattr(llm, 'chat', fail)
    response = client.post(ROOT + '/suggest', json=body())
    assert response.status_code == status, response.text
    assert PRIVATE not in response.text and 'API_KEY' not in response.text
    assert any('\u4e00' <= c <= '\u9fff' for c in response.json()['detail'])
    with closing(db.connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM ontology_mapping_versions').fetchone()[0] == 0


def test_evidence_invalidated_or_cross_workspace_never_rebinds(client):
    row = client.post(ROOT + '/suggest', json=body()).json()
    other = client.get(f"{ROOT}/{row['id']}/evidence", params={'workspace_key': 'studypal'})
    assert other.status_code == 404
    with closing(db.connect()) as conn, conn:
        conn.execute('UPDATE evidence SET stale=1')
        conn.execute('''INSERT INTO evidence(source_id,snapshot_id,locator_type,locator_start,locator_end,content)
                        VALUES(1,1,'line',3,4,'New valid evidence cannot impersonate the old binding')''')
    response = client.get(f"{ROOT}/{row['id']}/evidence", params={'workspace_key': 'workpal'})
    assert response.status_code == 409, response.text


def test_unknown_source_profile_and_workspace_are_not_disclosed(client):
    assert client.post(ROOT + '/suggest', json=body(source_id=999)).status_code == 404
    assert client.post(ROOT + '/suggest', json=body(profile_id='missing')).status_code == 422
    assert client.post(ROOT + '/suggest', json=body(workspace_key='missing')).status_code == 404


@pytest.mark.parametrize('operation', ['submit', 'approve'])
def test_sample_preview_does_not_grant_review_after_development_doc_reclassification(client, operation):
    row = client.post(ROOT + '/suggest', json=body()).json()
    command = {'workspace_key': 'workpal', 'expected_hash': row['mapping_hash']}
    assert client.get(f"{ROOT}/{row['id']}/evidence", params={'workspace_key': 'workpal'}).status_code == 200
    if operation == 'approve':
        assert client.post(f"{ROOT}/{row['id']}/submit", json=command).status_code == 200
    with closing(db.connect()) as conn, conn:
        conn.execute("UPDATE source_files SET source_kind='development_doc'")
    kwargs = {**command, 'confirmed': True} if operation == 'approve' else command
    response = client.post(f"{ROOT}/{row['id']}/{operation}", json=kwargs)
    assert response.status_code == 409, response.text

