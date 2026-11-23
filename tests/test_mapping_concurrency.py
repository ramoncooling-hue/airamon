"""Independent integration checks for competing mapping reviewers."""

import importlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier

import pytest

from app.memory import db


@pytest.fixture
def payload(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'APP_DIR', tmp_path)
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'test.db')
    db.apply_schema()
    with closing(db.connect()) as conn, conn:
        ws = conn.execute("SELECT id FROM workspaces WHERE key='workpal'").fetchone()[0]
        course = conn.execute("INSERT INTO courses(name,workspace_id) VALUES('Concurrent Work',?)", (ws,)).lastrowid
        source = conn.execute("INSERT INTO source_files(course_id,path,sha256,status) VALUES(?,'source.md',?,'indexed')",
                              (course, 'a' * 64)).lastrowid
        snapshot = conn.execute('INSERT INTO source_snapshots(source_id,sha256) VALUES(?,?)', (source, 'a' * 64)).lastrowid
        evidence = conn.execute('''INSERT INTO evidence(source_id,snapshot_id,locator_type,locator_start,
                                   locator_end,content) VALUES(?,?,'line',1,1,'Real sample')''', (source, snapshot)).lastrowid
    return {
        'ontology_version': 'personal@1.0.0', 'profile_id': 'work', 'profile_version': '1.0.0',
        'branch_keys': ['work.industry_knowledge'], 'node_types': ['method'], 'relation_kinds': ['supports'],
        'field_mappings': {'title': 'name'}, 'evidence_required': True,
        'sample_refs': [f'evidence:{evidence}'], 'model_id': 'local-test', 'prompt_version': 'mapping-v1',
    }


def service():
    return importlib.import_module('app.memory.ontology_mappings')


def test_concurrent_identical_creation_has_one_identity(payload):
    gate = Barrier(2)

    def create(_):
        gate.wait(timeout=5)
        return service().create_mapping('workpal', payload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        versions = list(pool.map(create, range(2)))
    assert versions[0]['id'] == versions[1]['id']
    assert versions[0]['mapping_hash'] == versions[1]['mapping_hash']
    with closing(db.connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM ontology_mapping_versions').fetchone()[0] == 1


def test_two_reviewers_cannot_both_approve_competing_baselines(payload):
    store = service()
    first = store.create_mapping('workpal', payload)
    second = store.create_mapping('workpal', dict(payload, field_mappings={'title': 'gist'}))
    for record in (first, second):
        store.submit_mapping(record['id'], workspace_key='workpal', expected_hash=record['mapping_hash'])
    gate = Barrier(2)

    def approve(record):
        gate.wait(timeout=5)
        try:
            return store.approve_mapping(record['id'], 'local_user', workspace_key='workpal',
                                         expected_hash=record['mapping_hash'])['status']
        except store.MappingConflict:
            return 'conflict'

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(approve, (first, second)))
    assert sorted(statuses) == ['approved', 'conflict']
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ontology_mapping_versions WHERE status='approved'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM ontology_mapping_versions WHERE status='pending_review'").fetchone()[0] == 1
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
