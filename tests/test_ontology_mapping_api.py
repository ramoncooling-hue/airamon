"""Mapping HTTP lifecycle uses real scoped evidence and an isolated database."""

import hashlib
import importlib
import re
from contextlib import closing

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import ontology_routes
from app.api.routes import router
from app.memory import db


ROOT = '/api/knowledge/ontology/mappings'
LOCAL_BASE = 'http://localhost:8765'
LOCAL_CLIENT = ('127.0.0.1', 43210)
PRIVATE = 'PRIVATE_MAPPING_INPUT_SENTINEL'


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'APP_DIR', tmp_path)
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'mapping-api.db')
    db.apply_schema()
    with closing(db.connect()) as conn, conn:
        conn.execute("INSERT INTO workspaces(key,name,kind) VALUES('team-x','Other work','work')")
        conn.execute("INSERT INTO workspaces(key,name,kind,status) VALUES('retired','Retired','work','archived')")
        workspace_id = conn.execute("SELECT id FROM workspaces WHERE key='workpal'").fetchone()[0]
        course_id = conn.execute(
            "INSERT INTO courses(name,workspace_id) VALUES('Mapping source',?)", (workspace_id,)
        ).lastrowid
        for index in (1, 2):
            sha = 'a' * 64
            source_id = conn.execute(
                "INSERT INTO source_files(course_id,path,sha256,status) VALUES(?,?,?,'indexed')",
                (course_id, f'mapping-source-{index}.md', sha),
            ).lastrowid
            snapshot_id = conn.execute(
                'INSERT INTO source_snapshots(source_id,sha256) VALUES(?,?)', (source_id, sha)
            ).lastrowid
            content = f'Indexed evidence claim {index}'
            conn.execute(
                '''INSERT INTO evidence(source_id,snapshot_id,locator_type,locator_start,
                                        locator_end,content,content_hash)
                   VALUES(?,?,'line',1,2,?,?)''',
                (source_id, snapshot_id, content, hashlib.sha256(content.encode()).hexdigest()),
            )
    application = FastAPI()
    application.include_router(router)
    with TestClient(application, base_url=LOCAL_BASE, client=LOCAL_CLIENT) as value:
        yield value


def mapping(**changes):
    result = {
        'ontology_version': 'personal@1.0.0', 'profile_id': 'work', 'profile_version': '1.0.0',
        'branch_keys': ['work.industry_knowledge'], 'node_types': ['method'],
        'relation_kinds': ['supports'], 'field_mappings': {'title': 'name', 'body': 'gist'},
        'evidence_required': True, 'sample_refs': ['evidence:1'],
        'model_id': 'test-model', 'prompt_version': 'mapping-v1',
    }
    result.update(changes)
    return result


def create(client, proposal=None, **changes):
    body = {'workspace_key': 'workpal', 'mapping': proposal or mapping(), **changes}
    response = client.post(ROOT, json=body)
    assert response.status_code == 200, response.text
    row = response.json()
    assert type(row['id']) is int and row['id'] > 0
    assert isinstance(row['mapping'], dict)
    assert 'mapping_json' not in row
    assert re.fullmatch('[0-9a-f]{64}', row['mapping_hash'])
    return row


def command(client, row, action, **changes):
    body = {'workspace_key': 'workpal', 'expected_hash': row['mapping_hash'], **changes}
    return client.post(f"{ROOT}/{row['id']}/{action}", json=body)


def approve(client, row):
    submitted = command(client, row, 'submit')
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()['status'] == 'pending_review'
    approved = command(client, row, 'approve', confirmed=True)
    assert approved.status_code == 200, approved.text
    assert approved.json()['status'] == 'approved'
    assert approved.json()['approved_by'] == 'local_user'
    return approved.json()


def graph_state():
    with closing(db.connect()) as conn:
        return {
            table: [tuple(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY rowid')]
            for table in ('kp', 'kp_edges', 'kp_evidence', 'knowledge_candidates',
                          'candidate_reviews', 'knowledge_documents')
        }


def test_create_duplicate_submit_approve_does_not_write_knowledge(client):
    before = graph_state()
    row = create(client)
    assert row['status'] == 'draft'
    assert row['created_by'] == 'local_user'
    assert create(client) == row
    approved = approve(client, row)
    read = client.get(f"{ROOT}/{row['id']}", params={'workspace_key': 'workpal'})
    assert read.status_code == 200
    assert read.json() == approved
    assert graph_state() == before


def test_list_default_limit_and_workspace_isolation(client):
    first = create(client)
    second = create(client, mapping(prompt_version='mapping-v2'))
    listed = client.get(ROOT, params={'workspace_key': 'workpal'})
    assert listed.status_code == 200
    assert {item['id'] for item in listed.json()} == {first['id'], second['id']}
    limited = client.get(ROOT, params={'workspace_key': 'workpal', 'limit': 1})
    assert limited.status_code == 200
    assert len(limited.json()) == 1
    empty = client.get(ROOT, params={'workspace_key': 'team-x'})
    assert empty.status_code == 200
    assert empty.json() == []
    assert client.get(f"{ROOT}/{first['id']}", params={'workspace_key': 'team-x'}).status_code == 404
    for action in ('submit', 'approve', 'reject'):
        extra = {'confirmed': True} if action == 'approve' else {}
        result = command(client, first, action, workspace_key='team-x', **extra)
        assert result.status_code == 404, result.text
    assert client.get(f"{ROOT}/{first['id']}", params={'workspace_key': 'workpal'}).json()['status'] == 'draft'


@pytest.mark.parametrize('workspace_key', ['missing', 'retired'])
def test_unknown_or_archived_scope_is_404(client, workspace_key):
    assert client.get(ROOT, params={'workspace_key': workspace_key}).status_code == 404
    assert client.get(f'{ROOT}/999', params={'workspace_key': workspace_key}).status_code == 404
    result = client.post(ROOT, json={'workspace_key': workspace_key, 'mapping': mapping()})
    assert result.status_code == 404


def test_missing_version_is_404(client):
    assert client.get(f'{ROOT}/999', params={'workspace_key': 'workpal'}).status_code == 404
    row = {'id': 999, 'mapping_hash': 'a' * 64}
    for action in ('submit', 'approve', 'reject'):
        extra = {'confirmed': True} if action == 'approve' else {}
        assert command(client, row, action, **extra).status_code == 404


def test_distinct_source_scopes_can_be_approved_together(client):
    first = approve(client, create(client))
    second = approve(client, create(client, mapping(sample_refs=['evidence:2'])))
    assert first['source_scope_hash'] != second['source_scope_hash']
    result = client.get(ROOT, params={'workspace_key': 'workpal'})
    assert {row['id'] for row in result.json() if row['status'] == 'approved'} == {first['id'], second['id']}


def test_revision_supersedes_only_matching_source_scope(client):
    first = approve(client, create(client))
    other = approve(client, create(client, mapping(sample_refs=['evidence:2'])))
    revision = create(client, mapping(prompt_version='mapping-v2'), base_version_id=first['id'])
    approve(client, revision)
    assert client.get(f"{ROOT}/{first['id']}", params={'workspace_key': 'workpal'}).json()['status'] == 'superseded'
    assert client.get(f"{ROOT}/{other['id']}", params={'workspace_key': 'workpal'}).json()['status'] == 'approved'


def test_rejected_revision_requires_new_draft_using_approved_baseline(client):
    baseline = approve(client, create(client))
    revision = create(client, mapping(prompt_version='rejected-v2'), base_version_id=baseline['id'])
    assert command(client, revision, 'submit').status_code == 200
    rejected = command(client, revision, 'reject', note='Needs review')
    assert rejected.status_code == 200
    assert rejected.json()['status'] == 'rejected'
    assert command(client, revision, 'approve', confirmed=True).status_code == 409
    corrected = create(client, mapping(prompt_version='corrected-v3'), base_version_id=baseline['id'])
    assert corrected['id'] != revision['id']
    approve(client, corrected)


@pytest.mark.parametrize('action', ['submit', 'approve', 'reject'])
def test_wrong_hash_is_conflict_without_changing_state(client, action):
    row = create(client)
    if action != 'submit':
        assert command(client, row, 'submit').status_code == 200
    before = client.get(f"{ROOT}/{row['id']}", params={'workspace_key': 'workpal'}).json()
    extra = {'confirmed': True} if action == 'approve' else {}
    result = command(client, row, action, expected_hash='0' * 64, **extra)
    assert result.status_code == 409
    assert client.get(f"{ROOT}/{row['id']}", params={'workspace_key': 'workpal'}).json() == before


@pytest.mark.parametrize('confirmed', [None, False, 1, 1.0, 'true', 'True', [], {}])
def test_approval_requires_literal_boolean_true(client, confirmed):
    row = create(client)
    assert command(client, row, 'submit').status_code == 200
    extra = {} if confirmed is None else {'confirmed': confirmed}
    result = command(client, row, 'approve', **extra)
    assert result.status_code == 422
    assert client.get(f"{ROOT}/{row['id']}", params={'workspace_key': 'workpal'}).json()['status'] == 'pending_review'


@pytest.mark.parametrize('field', ['created_by', 'approved_by', 'origin', 'status'])
def test_create_rejects_caller_identity_and_approval_fields(client, field):
    result = client.post(ROOT, json={'workspace_key': 'workpal', 'mapping': mapping(), field: PRIVATE})
    assert result.status_code == 422
    assert PRIVATE not in result.text


@pytest.mark.parametrize('field', ['created_by', 'approved_by', 'origin', 'status'])
def test_approval_rejects_self_claimed_identity(client, field):
    row = create(client)
    assert command(client, row, 'submit').status_code == 200
    result = command(client, row, 'approve', confirmed=True, **{field: PRIVATE})
    assert result.status_code == 422
    assert PRIVATE not in result.text


@pytest.mark.parametrize('changes', [
    {'workspace_key': 1}, {'mapping': []}, {'base_version_id': True},
    {'base_version_id': 1.0}, {'base_version_id': '1'}, {'base_version_id': 0}, {'base_version_id': -1},
])
def test_create_body_is_strict(client, changes):
    result = client.post(ROOT, json={'workspace_key': 'workpal', 'mapping': mapping(), **changes})
    assert result.status_code == 422


@pytest.mark.parametrize('action', ['submit', 'approve', 'reject'])
@pytest.mark.parametrize('bad_hash', ['A' * 64, 'a' * 63, 'g' * 64, 1, None])
def test_command_hash_is_strict_lowercase_sha256(client, action, bad_hash):
    extra = {'confirmed': True} if action == 'approve' else {}
    result = command(client, {'id': 1, 'mapping_hash': bad_hash}, action, **extra)
    assert result.status_code == 422


@pytest.mark.parametrize('action', ['approve', 'reject'])
@pytest.mark.parametrize('note', [1, None, 'x' * 2001])
def test_review_note_is_a_bounded_strict_string(client, action, note):
    extra = {'confirmed': True} if action == 'approve' else {}
    result = command(client, {'id': 1, 'mapping_hash': 'a' * 64}, action, note=note, **extra)
    assert result.status_code == 422


@pytest.mark.parametrize('version_id', ['0', '-1', '1.0', 'true', '+1', '01'])
def test_version_path_requires_positive_integer(client, version_id):
    assert client.get(f'{ROOT}/{version_id}', params={'workspace_key': 'workpal'}).status_code == 422
    result = client.post(f'{ROOT}/{version_id}/submit', json={'workspace_key': 'workpal', 'expected_hash': 'a' * 64})
    assert result.status_code == 422


@pytest.mark.parametrize('path', ['', '/1'])
def test_read_scope_is_required(client, path):
    assert client.get(ROOT + path).status_code == 422


@pytest.mark.parametrize('limit', ['0', '-1', '1.5', 'true'])
def test_list_limit_is_positive_integer(client, limit):
    assert client.get(ROOT, params={'workspace_key': 'workpal', 'limit': limit}).status_code == 422


@pytest.mark.parametrize('standalone', [False, True])
@pytest.mark.parametrize('action', ['', '/1/submit', '/1/approve', '/1/reject'])
@pytest.mark.parametrize('access', ['remote', 'remote-host', 'cross-origin', 'cross-site'])
def test_mutations_enforce_local_access_without_main_lifespan(client, standalone, action, access):
    application = FastAPI()
    application.include_router(ontology_routes.router if standalone else router)
    base_url = 'http://192.0.2.1:8765' if access == 'remote-host' else LOCAL_BASE
    peer = ('192.0.2.2', 43210) if access == 'remote' else LOCAL_CLIENT
    headers = {'origin': 'https://other.example'} if access == 'cross-origin' else {}
    if access == 'cross-site':
        headers['sec-fetch-site'] = 'cross-site'
    body = {'workspace_key': 'workpal', 'mapping': mapping()} if not action else {
        'workspace_key': 'workpal', 'expected_hash': 'a' * 64,
    }
    if action.endswith('approve'):
        body['confirmed'] = True
    path = '/knowledge/ontology/mappings' if standalone else ROOT
    with TestClient(application, base_url=base_url, client=peer) as blocked:
        response = blocked.post(path + action, json=body, headers=headers)
    assert response.status_code == 403, response.text


@pytest.mark.parametrize('standalone', [False, True])
@pytest.mark.parametrize('suffix', ['', '/{version_id}'])
@pytest.mark.parametrize('access', ['remote', 'remote-host', 'cross-origin', 'cross-site'])
def test_mapping_reads_enforce_local_access_without_main_lifespan(client, standalone, suffix, access):
    row = create(client)
    application = FastAPI()
    application.include_router(ontology_routes.router if standalone else router)
    base_url = 'http://192.0.2.1:8765' if access == 'remote-host' else LOCAL_BASE
    peer = ('192.0.2.2', 43210) if access == 'remote' else LOCAL_CLIENT
    headers = {'origin': 'https://other.example'} if access == 'cross-origin' else {}
    if access == 'cross-site':
        headers['sec-fetch-site'] = 'cross-site'
    path = '/knowledge/ontology/mappings' if standalone else ROOT
    with TestClient(application, base_url=base_url, client=peer) as blocked:
        response = blocked.get(path + suffix.format(version_id=row['id']),
                               params={'workspace_key': 'workpal'}, headers=headers)
    assert response.status_code == 403, response.text


@pytest.mark.parametrize('standalone', [False, True])
@pytest.mark.parametrize('suffix', ['', '/{version_id}'])
def test_same_origin_local_mapping_reads_are_allowed(client, standalone, suffix):
    row = create(client)
    application = FastAPI()
    application.include_router(ontology_routes.router if standalone else router)
    path = '/knowledge/ontology/mappings' if standalone else ROOT
    with TestClient(application, base_url=LOCAL_BASE, client=LOCAL_CLIENT) as local:
        response = local.get(path + suffix.format(version_id=row['id']),
                             params={'workspace_key': 'workpal'}, headers={'origin': LOCAL_BASE})
    assert response.status_code == 200, response.text
    assert response.json() == (row if suffix else [row])


def test_standalone_same_origin_local_create_is_allowed(client):
    application = FastAPI()
    application.include_router(ontology_routes.router)
    with TestClient(application, base_url=LOCAL_BASE, client=LOCAL_CLIENT) as local:
        response = local.post('/knowledge/ontology/mappings',
                              json={'workspace_key': 'workpal', 'mapping': mapping()},
                              headers={'origin': LOCAL_BASE})
    assert response.status_code == 200, response.text


def test_real_invalid_mapping_error_does_not_reflect_private_material(client):
    proposal = mapping(field_mappings={'body': PRIVATE})
    response = client.post(ROOT, json={'workspace_key': 'workpal', 'mapping': proposal})
    assert response.status_code == 422
    assert PRIVATE not in response.text


@pytest.mark.parametrize('operation,method,path', [
    ('create_mapping', 'post', ''), ('list_mappings', 'get', ''),
    ('get_mapping', 'get', '/1'), ('submit_mapping', 'post', '/1/submit'),
    ('approve_mapping', 'post', '/1/approve'), ('reject_mapping', 'post', '/1/reject'),
])
@pytest.mark.parametrize('error,status', [('MappingNotFound', 404), ('MappingConflict', 409), ('ValueError', 422)])
def test_repository_errors_are_sanitized(client, monkeypatch, operation, method, path, error, status):
    module = importlib.import_module('app.memory.ontology_mappings')
    exception = ValueError if error == 'ValueError' else getattr(module, error)

    def fail(*args, **kwargs):
        raise exception(PRIVATE)

    monkeypatch.setattr(module, operation, fail)
    if method == 'get':
        response = client.get(ROOT + path, params={'workspace_key': 'workpal'})
    else:
        body = {'workspace_key': 'workpal', 'expected_hash': 'a' * 64}
        if not path:
            body = {'workspace_key': 'workpal', 'mapping': mapping()}
        elif path.endswith('approve'):
            body['confirmed'] = True
        response = client.post(ROOT + path, json=body)
    assert response.status_code == status, response.text
    assert PRIVATE not in response.text
