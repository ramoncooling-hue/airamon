"""Explicit local extraction, isolated real files and synthetic model output only."""

import hashlib
import importlib
import json
from contextlib import closing

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent import llm
from app.api import ontology_routes
from app.memory import db, ontology_mappings

ROOT = '/api/knowledge/ontology/extractions'
BASE = 'http://localhost:8765'
PRIVATE = 'PRIVATE_EXTRACTION_SENTINEL'


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'APP_DIR', tmp_path)
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'extraction-api.db')
    db.apply_schema()
    material = tmp_path / 'synthetic.md'
    material.write_text('Evidence-backed method.\nAnother method.', encoding='utf-8')
    digest = hashlib.sha256(material.read_bytes()).hexdigest()
    with closing(db.connect()) as conn, conn:
        owner = conn.execute("SELECT id FROM workspaces WHERE key='workpal'").fetchone()[0]
        course = conn.execute("INSERT INTO courses(name,workspace_id) VALUES('Synthetic',?)", (owner,)).lastrowid
        source = conn.execute("INSERT INTO source_files(course_id,path,sha256,status,source_kind) "
                              "VALUES(?,?,?,'indexed','wiki_page')", (course, str(material), digest)).lastrowid
        snapshot = conn.execute('INSERT INTO source_snapshots(source_id,sha256) VALUES(?,?)',
                                (source, digest)).lastrowid
        evidence = conn.execute('INSERT INTO evidence(source_id,snapshot_id,locator_type,locator_start,locator_end,content) '
                                "VALUES(?,?,'line',1,1,'Evidence-backed method.')", (source, snapshot)).lastrowid
    mapping = ontology_mappings.create_mapping('workpal', {
        'ontology_version': 'personal@1.0.0', 'profile_id': 'work', 'profile_version': '1.0.0',
        'branch_keys': ['work.industry_knowledge'], 'node_types': ['method'],
        'relation_kinds': ['supports'], 'field_mappings': {'body': 'gist'},
        'evidence_required': True, 'sample_refs': [f'evidence:{evidence}'],
        'model_id': 'synthetic', 'prompt_version': 'synthetic-v1',
    })
    ontology_mappings.submit_mapping(mapping['id'], workspace_key='workpal', expected_hash=mapping['mapping_hash'])
    ontology_mappings.approve_mapping(mapping['id'], 'local_user', workspace_key='workpal',
                                     expected_hash=mapping['mapping_hash'])
    calls = []
    def chat(*args, **kwargs):
        calls.append((args, kwargs))
        return json.dumps({'candidates': [{
            'id': 'm1', 'name': 'Synthetic method', 'kp_type': 'method',
            'branch_keys': ['work.industry_knowledge'], 'gist': 'Evidence-backed method.',
            'detail': '', 'chapter': '', 'quote': 'Evidence-backed method.',
        }], 'relations': []})
    monkeypatch.setattr(llm, 'chat', chat)
    monkeypatch.setattr(llm, '_model_config', lambda *a, **k: pytest.fail('No real provider or settings'))
    application = FastAPI()
    application.include_router(ontology_routes.router, prefix='/api')
    with TestClient(application, base_url=BASE, client=('127.0.0.1', 43210)) as client:
        yield {'client': client, 'source': source, 'mapping': mapping, 'calls': calls, 'file': material,
               'application': application}


def body(world, **changes):
    return {'workspace_key': 'workpal', 'mapping_version_id': world['mapping']['id'],
            'mapping_hash': world['mapping']['mapping_hash'], 'source_ids': [world['source']], **changes}


def create(world):
    response = world['client'].post(ROOT, json=body(world))
    assert response.status_code == 200, response.text
    return response.json()


def protected_state():
    with closing(db.connect()) as conn:
        return {table: [tuple(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                for table in ('kp', 'kp_edges', 'kp_evidence', 'knowledge_candidates', 'candidate_reviews')}


def test_explicit_create_step_preview_and_replay_never_publish(world):
    before = protected_state()
    run = create(world)
    assert run['coverage']['total'] == 1 and not world['calls']
    assert create(world)['id'] == run['id']
    client = world['client']
    listing = client.get(ROOT, params={'workspace_key': 'workpal', 'mapping_version_id': world['mapping']['id']})
    assert listing.status_code == 200 and listing.json()[0]['id'] == run['id']
    response = client.post(f"{ROOT}/{run['id']}/step", json={'workspace_key': 'workpal'})
    assert response.status_code == 200, response.text
    done = response.json()
    assert done['status'] == 'completed' and done['coverage']['succeeded'] == 1
    assert len(done['candidates']) == 1 and len(world['calls']) == 1
    assert client.post(f"{ROOT}/{run['id']}/step", json={'workspace_key': 'workpal'}).status_code == 200
    assert len(world['calls']) == 1
    assert protected_state() == before


@pytest.mark.parametrize('changes', [
    {'mapping_version_id': True}, {'mapping_version_id': '1'}, {'mapping_version_id': 0},
    {'mapping_hash': 'A' * 64}, {'source_ids': [True]}, {'source_ids': ['1']},
    {'source_ids': []}, {'source_ids': [1, 1]}, {'source_ids': [2**63]},
    {'source_ids': list(range(1, 66))},
    {'workspace_key': PRIVATE, 'approved_by': PRIVATE}, {'mapping': {'status': 'approved'}},
])
def test_strict_creation_never_reflects_invalid_input(world, changes):
    response = world['client'].post(ROOT, json=body(world, **changes))
    assert response.status_code == 422, response.text
    assert PRIVATE not in response.text and not world['calls']


@pytest.mark.parametrize('suffix', ['', '/1/step', '/1/cancel', '/1/retry'])
@pytest.mark.parametrize('raw', [
    '{"workspace_key":"workpal","workspace_key":"PRIVATE_EXTRACTION_SENTINEL"}',
    '{"workspace_key":"workpal","source_ids":[NaN]}',
    '{"workspace_key":' + '[' * 1200 + ']' * 1200 + '}',
])
def test_duplicate_nonfinite_or_deep_json_is_rejected(world, suffix, raw):
    response = world['client'].post(ROOT + suffix, content=raw, headers={'content-type': 'application/json'})
    assert response.status_code == 422, response.text
    assert PRIVATE not in response.text


@pytest.mark.parametrize('method,suffix', [('get', ''), ('get', '/1'), ('post', ''),
                                         ('post', '/1/step'), ('post', '/1/cancel'), ('post', '/1/retry')])
@pytest.mark.parametrize('access', ['remote', 'host', 'origin', 'cross-site'])
def test_all_extraction_routes_local_only_even_standalone(world, method, suffix, access):
    peer = ('192.0.2.2', 43210) if access == 'remote' else ('127.0.0.1', 43210)
    base = 'http://192.0.2.1:8765' if access == 'host' else BASE
    headers = {'origin': 'https://evil.example'} if access == 'origin' else {}
    if access == 'cross-site':
        headers['sec-fetch-site'] = 'cross-site'
    with TestClient(world['application'], base_url=base, client=peer) as client:
        if method == 'get':
            response = client.get(ROOT + suffix, params={'workspace_key': 'workpal', 'mapping_version_id': 1}, headers=headers)
        else:
            response = client.post(ROOT + suffix, json=body(world) if not suffix else {'workspace_key': 'workpal'}, headers=headers)
    assert response.status_code == 403, response.text
    assert not world['calls']


def test_cross_workspace_cannot_read_step_or_cancel_existing_run(world):
    run = create(world)
    assert world['client'].get(f"{ROOT}/{run['id']}", params={'workspace_key': 'studypal'}).status_code == 404
    for action in ('step', 'cancel', 'retry'):
        response = world['client'].post(f"{ROOT}/{run['id']}/{action}", json={'workspace_key': 'studypal'})
        assert response.status_code == 404
    assert not world['calls']


def test_changed_original_file_is_conflict_before_model(world):
    run = create(world)
    world['file'].write_text('Changed original', encoding='utf-8')
    response = world['client'].post(f"{ROOT}/{run['id']}/step", json={'workspace_key': 'workpal'})
    assert response.status_code == 409, response.text
    assert not world['calls']


@pytest.mark.parametrize('mutation', ['evidence', 'file'])
def test_change_during_prompt_build_is_conflict_without_model_call(world, monkeypatch, mutation):
    output = importlib.import_module('app.memory.extraction_output')
    original = output.build_messages
    run = create(world)
    before = protected_state()

    def build_and_change(**kwargs):
        messages = original(**kwargs)
        if mutation == 'file':
            world['file'].write_text(PRIVATE, encoding='utf-8')
        else:
            with closing(db.connect()) as conn, conn:
                conn.execute('UPDATE evidence SET content=? WHERE source_id=?',
                             (PRIVATE, world['source']))
        return messages

    monkeypatch.setattr(output, 'build_messages', build_and_change)
    response = world['client'].post(f"{ROOT}/{run['id']}/step", json={'workspace_key': 'workpal'})
    assert response.status_code == 409, response.text
    assert not world['calls']
    assert PRIVATE not in response.text and str(world['file']) not in response.text
    with closing(db.connect()) as conn:
        assert conn.execute('SELECT status FROM mapping_extraction_runs WHERE id=?',
                            (run['id'],)).fetchone()[0] == 'conflict'
        assert conn.execute('SELECT COUNT(*) FROM mapping_extraction_segments '
                            "WHERE run_id=? AND (status='running' OR claim_token IS NOT NULL)",
                            (run['id'],)).fetchone()[0] == 0
    assert protected_state() == before


@pytest.mark.parametrize('content', ['', ' \t\n\u3000'])
@pytest.mark.parametrize('replay', [False, True])
def test_blank_live_evidence_create_or_replay_is_atomic_conflict(world, content, replay):
    if replay:
        create(world)
    before = protected_state()
    with closing(db.connect()) as conn, conn:
        ledger = {table: [tuple(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY id')]
                  for table in ('mapping_extraction_runs', 'mapping_extraction_segments')}
        snapshot = conn.execute('SELECT id FROM source_snapshots WHERE source_id=?',
                                (world['source'],)).fetchone()[0]
        conn.execute('INSERT INTO evidence(source_id,snapshot_id,stale,content) VALUES(?,?,0,?)',
                     (world['source'], snapshot, content))
    response = world['client'].post(ROOT, json=body(world))
    assert response.status_code == 409, response.text
    assert not world['calls'] and str(world['file']) not in response.text
    with closing(db.connect()) as conn:
        for table, rows in ledger.items():
            assert [tuple(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY id')] == rows
    assert protected_state() == before


@pytest.mark.parametrize('name,status', [('ExtractionNotFound', 404), ('ExtractionConflict', 409),
                                      ('ValueError', 422), ('OSError', 409), ('LLMError', 502)])
def test_internal_error_does_not_echo_paths_or_provider_values(world, monkeypatch, name, status):
    module = importlib.import_module('app.memory.mapping_extraction')
    exception = {'ValueError': ValueError, 'OSError': OSError, 'LLMError': llm.LLMError}.get(name)
    exception = exception or getattr(module, name)
    def fail(*args, **kwargs):
        raise exception(PRIVATE)
    monkeypatch.setattr(module, 'create_run', fail)
    response = world['client'].post(ROOT, json=body(world))
    assert response.status_code == status, response.text
    assert PRIVATE not in response.text
