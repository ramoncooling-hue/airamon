"""Contract endpoints validate proposals without changing personal knowledge."""

import copy
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import router
from app.memory import db


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'APP_DIR', tmp_path)
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'test.db')
    db.apply_schema()
    with db.connect() as conn:
        conn.execute("INSERT INTO workspaces(key,name,kind) VALUES('team-x','Custom Work','work')")
        conn.execute("INSERT INTO workspaces(key,name,kind) VALUES('retired','Retired','work')")
        conn.execute("UPDATE workspaces SET status='archived' WHERE key='retired'")
    application = FastAPI()
    application.include_router(router)
    with TestClient(application) as value:
        yield value


def mapping():
    return {
        'ontology_version': 'personal@1.0.0', 'profile_id': 'work', 'profile_version': '1.0.0',
        'branch_keys': ['work.industry_knowledge'], 'node_types': ['method'],
        'relation_kinds': ['supports'], 'field_mappings': {'title': 'name', 'body': 'gist'},
        'evidence_required': True, 'sample_refs': ['sample-1'],
        'model_id': 'test-model', 'prompt_version': 'mapping-v1',
    }


def test_registry_is_available_on_real_router(client):
    response = client.get('/api/knowledge/ontology')
    assert response.status_code == 200
    assert response.json()['ontology_version'] == 'personal@1.0.0'


def test_mapping_validation_is_repeatable_and_never_approval(client):
    body = {'workspace_key': 'team-x', 'mapping': mapping()}
    monitor = sqlite3.connect(db.DB_PATH)
    try:
        before = monitor.execute('PRAGMA data_version').fetchone()[0]
        first = client.post('/api/knowledge/ontology/validate-mapping', json=body)
        second = client.post('/api/knowledge/ontology/validate-mapping', json=body)
        assert monitor.execute('PRAGMA data_version').fetchone()[0] == before
    finally:
        monitor.close()
    assert first.status_code == 200, first.text
    assert first.json() == second.json()
    assert first.json()['mapping']['status'] == 'pending_review'
    assert first.json()['workspace_key'] == 'team-x'
    assert first.json()['semantic_support_checked'] is False


def test_json_schema_is_available_to_model_adapters(client):
    result = client.get('/api/knowledge/ontology/mapping-schema')
    assert result.status_code == 200
    assert result.json()['type'] == 'object'
    assert result.json()['additionalProperties'] is False


@pytest.mark.parametrize('workspace', ['missing', 'retired'])
def test_missing_or_archived_workspace_is_rejected(client, workspace):
    result = client.post('/api/knowledge/ontology/validate-mapping',
                         json={'workspace_key': workspace, 'mapping': mapping()})
    assert result.status_code == 404


def test_scope_comes_from_database_not_model(client):
    proposal = mapping()
    proposal.update(profile_id='mba', branch_keys=['study.knowledge_understanding'])
    result = client.post('/api/knowledge/ontology/validate-mapping',
                         json={'workspace_key': 'team-x', 'mapping': proposal})
    assert result.status_code == 422


def test_caller_cannot_override_kind_or_self_approve(client):
    body = {'workspace_key': 'team-x', 'workspace_kind': 'study', 'mapping': mapping()}
    assert client.post('/api/knowledge/ontology/validate-mapping', json=body).status_code == 422
    del body['workspace_kind']
    body['mapping']['status'] = 'approved'
    assert client.post('/api/knowledge/ontology/validate-mapping', json=body).status_code == 422


def test_bad_mapping_returns_no_trace_or_raw_material(client):
    body = {'workspace_key': 'team-x', 'mapping': copy.deepcopy(mapping())}
    body['mapping']['field_mappings']['body'] = 'PRIVATE_SENTINEL_UNSUPPORTED_FIELD'
    result = client.post('/api/knowledge/ontology/validate-mapping', json=body)
    assert result.status_code == 422
    assert 'PRIVATE_SENTINEL' not in result.text
