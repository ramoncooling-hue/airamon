"""Immutable mapping versions and scoped review, using only temporary databases."""

import copy
import hashlib
import importlib
import json
import socket
import sqlite3
from contextlib import closing
from functools import lru_cache

import pytest

from app.memory import db, domain_profiles, ontology


def service():
    return importlib.import_module('app.memory.ontology_mappings')


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'APP_DIR', tmp_path / 'app')
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'app' / 'mappings.db')
    db.apply_schema()
    samples = []
    with closing(db.connect()) as conn, conn:
        workspace = conn.execute("SELECT id FROM workspaces WHERE key='workpal'").fetchone()[0]
        course = conn.execute("INSERT INTO courses(name,workspace_id) VALUES('Mapping samples',?)",
                              (workspace,)).lastrowid
        for index in range(2):
            sha = str(index + 1) * 64
            source = conn.execute(
                "INSERT INTO source_files(course_id,path,sha256,status) VALUES(?,?,?,'indexed')",
                (course, f'sample-{index}.md', sha)).lastrowid
            snapshot = conn.execute('INSERT INTO source_snapshots(source_id,sha256) VALUES(?,?)',
                                    (source, sha)).lastrowid
            evidence = conn.execute(
                """INSERT INTO evidence(source_id,snapshot_id,locator_type,locator_start,
                   locator_end,content,content_hash) VALUES(?,?,'line',1,2,'Original claim',?)""",
                (source, snapshot, hashlib.sha256(b'Original claim').hexdigest())).lastrowid
            samples.append({'source': source, 'snapshot': snapshot, 'evidence': evidence})
    domain_profiles.load_profiles()
    def forbidden(*args, **kwargs):
        raise AssertionError('Mapping persistence must not call the network')
    monkeypatch.setattr(socket, 'socket', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    return samples


def mapping(world, sample=0, **changes):
    result = {
        'ontology_version': ontology.ONTOLOGY_VERSION,
        'profile_id': 'work', 'profile_version': '1.0.0',
        'branch_keys': ['work.industry_knowledge'],
        'node_types': ['method'], 'relation_kinds': ['supports'],
        'field_mappings': {'title': 'name', 'body': 'gist'},
        'evidence_required': True, 'sample_refs': [f"evidence:{world[sample]['evidence']}"],
        'model_id': 'test-model', 'prompt_version': 'mapping-v1',
    }
    result.update(changes)
    return result


def create(world, sample=0, base=None, **changes):
    return service().create_mapping('workpal', mapping(world, sample, **changes),
                                    base_version_id=base)


def transition(name, row, **kwargs):
    return getattr(service(), name)(row['id'], workspace_key=row['workspace_key'],
                                   expected_hash=row['mapping_hash'], **kwargs)


def approved(world, **kwargs):
    row = create(world, **kwargs)
    transition('submit_mapping', row)
    return transition('approve_mapping', row, approved_by='reviewer', note='Accepted')


def test_create_is_draft_canonical_idempotent_and_independent(world):
    payload = mapping(world)
    payload['model_id'] = ' test-model '
    row = service().create_mapping('workpal', payload)
    assert row['status'] == 'draft'
    assert row['created_by'] == 'user'
    assert row['approved_by'] is row['approved_at'] is row['rejected_at'] is None
    assert row['mapping'] == mapping(world)
    assert 'status' not in row['mapping']
    expected_keys = {'id', 'workspace_key', 'ontology_version', 'profile_id', 'profile_version',
                     'profile_hash', 'source_scope_hash', 'mapping', 'bindings', 'mapping_hash',
                     'status', 'base_version_id', 'created_by', 'approved_by', 'created_at',
                     'approved_at', 'rejected_at', 'note'}
    assert row.keys() == expected_keys
    source_ids = [world[0]['source']]
    canonical = json.dumps(source_ids, sort_keys=True, separators=(',', ':'))
    assert row['source_scope_hash'] == hashlib.sha256(canonical.encode()).hexdigest()
    assert create(world)['id'] == row['id']
    payload['field_mappings']['title'] = 'trap'
    row['mapping']['field_mappings']['title'] = 'trap'
    row['bindings'][0]['source_id'] = 999
    fresh = service().get_mapping(row['id'], workspace_key='workpal')
    assert fresh['mapping'] == mapping(world)
    assert fresh['bindings'][0]['source_id'] == world[0]['source']


@pytest.mark.parametrize('changes', [
    {'sample_refs': []}, {'sample_refs': ['evidence:01']}, {'sample_refs': ['sample-1']},
    {'branch_keys': ['study.goals_paths']}, {'ontology_version': 'old'},
    {'profile_version': '0.0.1'}, {'status': 'approved'}, {'model_id': float('nan')},
    {'model_id': object()},
])
def test_create_rejects_invalid_mapping_without_rows(world, changes):
    with pytest.raises(ValueError):
        create(world, **changes)
    assert service().list_mappings('workpal') == []


def test_create_requires_object_and_real_active_workspace_kind(world):
    for payload in (None, [], 'mapping'):
        with pytest.raises(ValueError):
            service().create_mapping('workpal', payload)
    with pytest.raises(ValueError):
        service().create_mapping('missing', mapping(world))
    with pytest.raises(ValueError):
        service().create_mapping('studypal', mapping(world))
    with closing(db.connect()) as conn, conn:
        conn.execute("UPDATE workspaces SET kind='study' WHERE key='workpal'")
    with pytest.raises(ValueError):
        create(world)


def test_submit_approve_and_repeated_create_preserve_version(world):
    row = create(world)
    pending = transition('submit_mapping', row)
    assert pending['status'] == 'pending_review'
    result = transition('approve_mapping', row, approved_by='reviewer', note='Accepted')
    assert result['status'] == 'approved'
    assert result['approved_by'] == 'reviewer'
    assert result['approved_at'] and result['note'] == 'Accepted'
    assert create(world, **{}) == result
    assert result['mapping'] == row['mapping']
    with pytest.raises(service().MappingConflict):
        transition('approve_mapping', row, approved_by='reviewer')


def test_reject_only_pending_and_never_revalidates_evidence(world, monkeypatch):
    row = create(world)
    with pytest.raises(service().MappingConflict):
        transition('reject_mapping', row)
    transition('submit_mapping', row)
    def forbidden(*args):
        raise AssertionError('Reject must not revalidate bindings')
    evidence = importlib.import_module('app.memory.mapping_evidence')
    monkeypatch.setattr(evidence, 'verify_bindings', forbidden)
    result = transition('reject_mapping', row, note='Needs correction')
    assert result['status'] == 'rejected' and result['rejected_at']
    assert result['note'] == 'Needs correction' and result['approved_at'] is None
    with pytest.raises(service().MappingConflict):
        transition('submit_mapping', row)
    with pytest.raises(service().MappingConflict):
        transition('reject_mapping', row)


def test_scoped_cas_and_invalid_transitions_do_not_write(world):
    row = create(world)
    with closing(db.connect()) as monitor:
        before = monitor.execute('PRAGMA data_version').fetchone()[0]
        for name in ('submit_mapping', 'approve_mapping', 'reject_mapping'):
            extra = {'approved_by': 'reviewer'} if name == 'approve_mapping' else {}
            with pytest.raises(service().MappingNotFound):
                getattr(service(), name)(row['id'], workspace_key='studypal',
                                         expected_hash=row['mapping_hash'], **extra)
            with pytest.raises(service().MappingConflict):
                getattr(service(), name)(row['id'], workspace_key='workpal',
                                         expected_hash='0' * 64, **extra)
        with pytest.raises(service().MappingConflict):
            transition('approve_mapping', row, approved_by='reviewer')
        with pytest.raises(service().MappingNotFound):
            service().get_mapping(row['id'], workspace_key='studypal')
        with pytest.raises(service().MappingNotFound):
            service().get_mapping(99999, workspace_key='workpal')
        assert monitor.execute('PRAGMA data_version').fetchone()[0] == before


@pytest.mark.parametrize('stage', ['submit_mapping', 'approve_mapping'])
@pytest.mark.parametrize('change', ['evidence', 'profile', 'ontology', 'kind', 'archived', 'missing'])
def test_review_revalidates_current_contract_workspace_and_bindings(world, monkeypatch, stage, change):
    row = create(world)
    if stage == 'approve_mapping':
        transition('submit_mapping', row)
    if change == 'profile':
        profiles = copy.deepcopy(domain_profiles.load_profiles())
        profiles['work']['quality_rules'].append('New rule with unchanged version')
        monkeypatch.setattr(domain_profiles, 'load_profiles', lru_cache(maxsize=1)(lambda: profiles))
    elif change == 'ontology':
        monkeypatch.setattr(ontology, 'ONTOLOGY_VERSION', 'personal@2.0.0')
    else:
        with closing(db.connect()) as conn, conn:
            if change == 'evidence':
                conn.execute('UPDATE evidence SET stale=1')
            elif change == 'kind':
                conn.execute("UPDATE workspaces SET kind='study' WHERE key='workpal'")
            elif change == 'archived':
                conn.execute("UPDATE workspaces SET status='archived' WHERE key='workpal'")
            else:
                conn.execute("UPDATE workspaces SET key='renamed' WHERE key='workpal'")
    extra = {'approved_by': 'reviewer'} if stage == 'approve_mapping' else {}
    error = service().MappingNotFound if change == 'missing' else service().MappingConflict
    with pytest.raises(error):
        transition(stage, row, **extra)
    assert service().get_mapping(row['id'], workspace_key='workpal')['status'] == (
        'draft' if stage == 'submit_mapping' else 'pending_review')


def test_same_version_profile_file_edit_is_detected_without_manual_cache_clear(world, tmp_path, monkeypatch):
    profiles = copy.deepcopy(domain_profiles.load_profiles())
    profile_dir = tmp_path / 'domains'
    profile_dir.mkdir()
    profile_file = profile_dir / 'work.json'
    profile_file.write_text(json.dumps(profiles['work']), encoding='utf-8')
    monkeypatch.setattr(domain_profiles, 'PROFILE_DIR', profile_dir)
    try:
        row = create(world)
        transition('submit_mapping', row)
        profiles['work']['quality_rules'].append('Same version, changed full contract')
        profile_file.write_text(json.dumps(profiles['work']), encoding='utf-8')
        with pytest.raises(service().MappingConflict):
            transition('approve_mapping', row, approved_by='reviewer')
        assert service().get_mapping(row['id'], workspace_key='workpal')['status'] == 'pending_review'
    finally:
        domain_profiles.clear_cache()


def test_new_source_snapshot_has_new_hash_same_scope_and_supersedes_atomically(world):
    old = approved(world)
    with closing(db.connect()) as conn, conn:
        sha = 'b' * 64
        conn.execute('UPDATE source_files SET sha256=? WHERE id=?', (sha, world[0]['source']))
        snapshot = conn.execute('INSERT INTO source_snapshots(source_id,sha256) VALUES(?,?)',
                                (world[0]['source'], sha)).lastrowid
        conn.execute('UPDATE evidence SET snapshot_id=? WHERE id=?', (snapshot, world[0]['evidence']))
    row = create(world, base=old['id'])
    assert row['mapping_hash'] != old['mapping_hash']
    assert row['source_scope_hash'] == old['source_scope_hash']
    transition('submit_mapping', row)
    new = transition('approve_mapping', row, approved_by='second reviewer')
    old_now = service().get_mapping(old['id'], workspace_key='workpal')
    assert old_now == {**old, 'status': 'superseded'}
    assert new['status'] == 'approved'
    assert len(service().list_mappings('workpal')) == 2


def test_different_sources_and_profiles_keep_coexisting_approvals(world):
    one = approved(world)
    two = approved(world, sample=1)
    general = approved(world, profile_id='general', node_types=['concept'])
    assert len({one['source_scope_hash'], two['source_scope_hash']}) == 2
    assert all(item['status'] == 'approved' for item in service().list_mappings('workpal'))
    assert general['source_scope_hash'] == one['source_scope_hash']


def test_source_scope_is_sorted_unique_source_ids(world):
    first = create(world, sample_refs=[f"evidence:{item['evidence']}" for item in reversed(world)])
    second = create(world, sample_refs=[f"evidence:{item['evidence']}" for item in world])
    assert first['source_scope_hash'] == second['source_scope_hash']
    assert first['id'] == second['id']


def test_two_reviewed_drafts_cannot_replace_a_newer_baseline(world):
    original = approved(world)
    one = create(world, base=original['id'], prompt_version='v2')
    two = create(world, base=original['id'], prompt_version='v3')
    transition('submit_mapping', one)
    transition('submit_mapping', two)
    transition('approve_mapping', one, approved_by='reviewer')
    with pytest.raises(service().MappingConflict):
        transition('approve_mapping', two, approved_by='reviewer')
    assert service().get_mapping(two['id'], workspace_key='workpal')['status'] == 'pending_review'
    assert service().get_mapping(one['id'], workspace_key='workpal')['status'] == 'approved'


def test_initial_reviewed_drafts_need_none_baseline_and_base_is_approved_only(world):
    draft = create(world)
    with pytest.raises(service().MappingConflict):
        create(world, base=draft['id'], prompt_version='v2')
    transition('submit_mapping', draft)
    transition('reject_mapping', draft)
    with pytest.raises(service().MappingConflict):
        create(world, base=draft['id'], prompt_version='v3')
    one = create(world, prompt_version='v2')
    two = create(world, prompt_version='v3')
    transition('submit_mapping', one)
    transition('submit_mapping', two)
    transition('approve_mapping', one, approved_by='reviewer')
    with pytest.raises(service().MappingConflict):
        transition('approve_mapping', two, approved_by='reviewer')
    with pytest.raises(ValueError):
        create(world, sample=1, base=one['id'])
    with pytest.raises(ValueError):
        create(world, base=one['id'], profile_id='general', node_types=['concept'])
    with pytest.raises(service().MappingNotFound):
        create(world, base=99999)


def test_read_helpers_do_not_write_and_limit_is_bounded(world):
    first = create(world)
    second = create(world, sample=1)
    with closing(db.connect()) as monitor:
        before = monitor.execute('PRAGMA data_version').fetchone()[0]
        assert service().get_mapping(first['id'], workspace_key='workpal') == first
        assert service().list_mappings('workpal', limit=1) == [second]
        assert service().list_mappings('studypal') == []
        for limit in (0, -1, True, '1', 1001):
            with pytest.raises(ValueError):
                service().list_mappings('workpal', limit=limit)
        assert monitor.execute('PRAGMA data_version').fetchone()[0] == before


def test_sql_defends_payload_review_history_and_transitions(world):
    row = create(world)
    with closing(db.connect()) as conn:
        mutations = [
            "mapping_json='{}'", "bindings_json='[]'", "mapping_hash='changed'",
            "profile_hash='changed'", "source_scope_hash='changed'", "created_by='other'",
            "workspace_key='studypal'", "profile_id='general'", "base_version_id=id",
            "created_at='changed'", "id=999", "ontology_version='changed'",
            "profile_version='changed'", "status='approved'", "status='unknown'",
        ]
        for assignment in mutations:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(f'UPDATE ontology_mapping_versions SET {assignment} WHERE id=?', (row['id'],))
            conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('DELETE FROM ontology_mapping_versions WHERE id=?', (row['id'],))
        conn.rollback()
    transition('submit_mapping', row)
    result = transition('approve_mapping', row, approved_by='reviewer', note='Final')
    with closing(db.connect()) as conn:
        for assignment in ("note='changed'", "approved_by='other'", "approved_at=NULL", "status='draft'"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(f'UPDATE ontology_mapping_versions SET {assignment} WHERE id=?', (row['id'],))
            conn.rollback()
    assert service().get_mapping(row['id'], workspace_key='workpal') == result


def test_failed_approval_rolls_back_superseding_previous_version(world):
    old = approved(world)
    row = create(world, base=old['id'], prompt_version='v2')
    transition('submit_mapping', row)
    with closing(db.connect()) as conn, conn:
        conn.execute("""CREATE TRIGGER fail_mapping_approval BEFORE UPDATE ON ontology_mapping_versions
                        WHEN NEW.status='approved' BEGIN SELECT RAISE(ABORT,'test failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        transition('approve_mapping', row, approved_by='reviewer')
    assert service().get_mapping(old['id'], workspace_key='workpal')['status'] == 'approved'
    assert service().get_mapping(row['id'], workspace_key='workpal')['status'] == 'pending_review'


@pytest.mark.parametrize('status', ['draft', 'pending_review', 'approved', 'rejected', 'superseded'])
@pytest.mark.parametrize('collision', ['id', 'workspace_hash'])
def test_sql_replace_cannot_rewrite_any_mapping_version(world, status, collision):
    row = create(world)
    if status != 'draft':
        transition('submit_mapping', row)
    if status in ('approved', 'superseded'):
        transition('approve_mapping', row, approved_by='reviewer', note='Keep review history')
    elif status == 'rejected':
        transition('reject_mapping', row, note='Keep rejection history')
    if status == 'superseded':
        approved(world, base=row['id'], prompt_version='replacement')
    with closing(db.connect()) as conn:
        conn.execute('PRAGMA recursive_triggers=OFF')
        assert conn.execute('PRAGMA recursive_triggers').fetchone()[0] == 0
        before = [dict(item) for item in conn.execute(
            'SELECT * FROM ontology_mapping_versions ORDER BY id')]
        replacement = next(dict(item) for item in before if item['id'] == row['id'])
        assert replacement['status'] == status
        replacement.update(status='draft', mapping_json='{}', approved_by=None,
                           approved_at=None, rejected_at=None, note='', base_version_id=None)
        if collision == 'id':
            replacement['mapping_hash'] = 'f' * 64
        else:
            replacement['id'] += 10000
        columns = ','.join(replacement)
        placeholders = ','.join('?' for _ in replacement)
        with pytest.raises(sqlite3.IntegrityError, match='Mapping version already exists'):
            conn.execute(f'INSERT OR REPLACE INTO ontology_mapping_versions ({columns}) '
                         f'VALUES ({placeholders})', tuple(replacement.values()))
        conn.rollback()
        assert [dict(item) for item in conn.execute(
            'SELECT * FROM ontology_mapping_versions ORDER BY id')] == before


@pytest.mark.parametrize('conflict_clause', ['OR REPLACE', 'OR IGNORE'])
def test_sql_update_conflict_cannot_displace_active_approval(world, conflict_clause):
    old = approved(world)
    pending = create(world, prompt_version='another-review')
    transition('submit_mapping', pending)
    with closing(db.connect()) as conn:
        conn.execute('PRAGMA recursive_triggers=OFF')
        assert conn.execute('PRAGMA recursive_triggers').fetchone()[0] == 0
        before = [dict(row) for row in conn.execute(
            'SELECT * FROM ontology_mapping_versions ORDER BY id')]
        with pytest.raises(sqlite3.IntegrityError, match='Approved mapping already exists in source scope'):
            conn.execute(
                f"""UPDATE {conflict_clause} ontology_mapping_versions SET status='approved',
                    approved_by='test',approved_at=CURRENT_TIMESTAMP,note='Replacement' WHERE id=?""",
                (pending['id'],))
        # Inspect before rollback: ABORT must preserve every row and review field.
        assert [dict(row) for row in conn.execute(
            'SELECT * FROM ontology_mapping_versions ORDER BY id')] == before
        conn.commit()
    assert service().get_mapping(old['id'], workspace_key='workpal') == old
    assert service().get_mapping(pending['id'], workspace_key='workpal')['status'] == 'pending_review'


def test_sql_update_replace_batch_conflict_preserves_all_review_records(world):
    old = approved(world)
    independent = create(world, sample=1)
    conflicting = create(world, prompt_version='another-review')
    transition('submit_mapping', independent)
    transition('submit_mapping', conflicting)
    with closing(db.connect()) as conn:
        conn.execute('PRAGMA recursive_triggers=OFF')
        assert conn.execute('PRAGMA recursive_triggers').fetchone()[0] == 0
        before = [dict(row) for row in conn.execute(
            'SELECT * FROM ontology_mapping_versions ORDER BY id')]
        with pytest.raises(sqlite3.IntegrityError, match='Approved mapping already exists in source scope'):
            conn.execute(
                """UPDATE OR REPLACE ontology_mapping_versions SET status='approved',
                   approved_by='test',approved_at=CURRENT_TIMESTAMP WHERE id IN (?,?)""",
                (independent['id'], conflicting['id']))
        assert [dict(row) for row in conn.execute(
            'SELECT * FROM ontology_mapping_versions ORDER BY id')] == before
        conn.commit()
    assert service().get_mapping(old['id'], workspace_key='workpal') == old
    assert all(service().get_mapping(row['id'], workspace_key='workpal')['status'] == 'pending_review'
               for row in (independent, conflicting))
