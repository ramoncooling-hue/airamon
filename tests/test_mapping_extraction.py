"""Durable extraction with isolated SQLite databases and real temporary files."""

import copy
import hashlib
import importlib
import json
import socket
import sqlite3
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier, Event

import pytest

from app.agent import llm
from app.memory import db, domain_profiles, ontology_mappings


def service():
    return importlib.import_module('app.memory.mapping_extraction')


def execute(sql, args=()):
    with closing(db.connect()) as conn, conn:
        return conn.execute(sql, args).lastrowid


@pytest.fixture
def world(tmp_path, monkeypatch):
    (tmp_path / 'db').mkdir()
    monkeypatch.setattr(db, 'APP_DIR', tmp_path / 'db')
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'db' / 'extraction.db')
    db.apply_schema()
    ws = None
    with closing(db.connect()) as conn:
        ws = conn.execute("SELECT id FROM workspaces WHERE key='workpal'").fetchone()[0]
    course = execute("INSERT INTO courses(name,workspace_id) VALUES('Synthetic extraction',?)", (ws,))
    path = tmp_path / 'synthetic.txt'
    path.write_text('Synthetic original file.\n', encoding='utf-8')
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    source = execute("INSERT INTO source_files(course_id,path,sha256,status) VALUES(?,?,?,'indexed')",
                     (course, str(path), sha))
    snapshot = execute('INSERT INTO source_snapshots(source_id,sha256) VALUES(?,?)', (source, sha))
    evidence = []
    for i in range(10):
        text = 'Evidence claim ' + str(i) + (' 文' * 4100 if i == 9 else '.')
        evidence.append(execute('''INSERT INTO evidence
            (source_id,snapshot_id,locator_type,locator_start,locator_end,content,content_hash)
            VALUES(?,?,'line',?,?,?,?)''', (source, snapshot, i, i + 1, text,
                                           hashlib.sha256(text.encode()).hexdigest())))
    payload = {
        'ontology_version': 'personal@1.0.0', 'profile_id': 'work', 'profile_version': '1.0.0',
        'branch_keys': ['work.industry_knowledge'], 'node_types': ['method', 'concept'],
        'relation_kinds': ['supports'], 'field_mappings': {'title': 'name', 'body': 'gist'},
        'evidence_required': True, 'sample_refs': [f'evidence:{evidence[0]}'],
        'model_id': 'synthetic', 'prompt_version': 'mapping-v1',
    }
    mapping = ontology_mappings.create_mapping('workpal', payload)
    ontology_mappings.submit_mapping(mapping['id'], workspace_key='workpal', expected_hash=mapping['mapping_hash'])
    mapping = ontology_mappings.approve_mapping(mapping['id'], 'tester', workspace_key='workpal',
                                               expected_hash=mapping['mapping_hash'])
    calls = []

    def chat(*args, **kwargs):
        calls.append((args, kwargs))
        return json.dumps({'candidates': [], 'relations': []})

    def forbidden(*args, **kwargs):
        raise AssertionError('Tests must never contact real services')

    monkeypatch.setattr(llm, 'chat', chat)
    monkeypatch.setattr(socket, 'socket', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    # Until the independently owned parser lands, use its exact interface.
    # Never replace the real parser if available in this checkout.
    if importlib.util.find_spec('app.memory.extraction_output') is None:
        stub = types.ModuleType('app.memory.extraction_output')
        stub.parse_output = lambda raw, *, mapping, evidence_text: json.loads(raw)
        stub.build_messages = lambda *, mapping, evidence_text: [{'role': 'user', 'content': evidence_text}]
        monkeypatch.setitem(sys.modules, 'app.memory.extraction_output', stub)
        import app.memory
        monkeypatch.setattr(app.memory, 'extraction_output', stub, raising=False)
    return dict(source=source, snapshot=snapshot, evidence=evidence, path=path, sha=sha,
                course=course, mapping=mapping, calls=calls, payload=payload)


def create(world, **changes):
    args = dict(workspace_key='workpal', mapping_version_id=world['mapping']['id'],
                mapping_hash=world['mapping']['mapping_hash'], source_ids=[world['source']])
    args.update(changes)
    return service().create_run(**args)


def action(name, run, **changes):
    return getattr(service(), name)(run['id'], workspace_key=changes.get('workspace_key', 'workpal'))


def counts():
    with closing(db.connect()) as conn:
        return {table: conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                for table in ('kp', 'kp_edges', 'knowledge_candidates', 'candidate_reviews')}


def candidate(name='Shared claim', **changes):
    result = dict(id='a', name=name, kp_type='method', branch_keys=['work.industry_knowledge'],
                  gist='A grounded statement', detail='Synthetic detail', chapter='', quote='Evidence')
    result.update(changes)
    return result


def model(monkeypatch, candidates=None, relations=None):
    monkeypatch.setattr(llm, 'chat', lambda *args, **kwargs: json.dumps({
        'candidates': candidates or [], 'relations': relations or []}))


def test_creation_covers_all_evidence_and_long_content_without_truncation(world):
    run = create(world)
    assert run['status'] == 'ready'
    assert run['coverage'] == dict(total=12, pending=12, running=0, succeeded=0, failed=0, cancelled=0)
    assert {s['evidence_id'] for s in run['segments']} == set(world['evidence'])
    pieces = [s for s in run['segments'] if s['evidence_id'] == world['evidence'][-1]]
    with closing(db.connect()) as conn:
        text = conn.execute('SELECT content FROM evidence WHERE id=?', (world['evidence'][-1],)).fetchone()[0]
    assert [(s['start'], s['end']) for s in pieces] == [(0, 4000), (4000, 8000), (8000, len(text))]
    assert world['calls'] == []
    assert run['candidates'] == run['relations'] == run['conflicts'] == []
    assert create(world) == run
    assert service().list_runs(workspace_key='workpal', mapping_version_id=world['mapping']['id']) == [run]


@pytest.mark.parametrize('binding', ['null', 'old_hash', 'cross_source'])
def test_creation_rejects_invalid_unsampled_live_evidence_and_rolls_back(world, binding):
    snapshot = None
    if binding == 'old_hash':
        old_sha = hashlib.sha256(b'old synthetic source version').hexdigest()
        assert old_sha != world['sha']
        snapshot = execute('INSERT INTO source_snapshots(source_id,sha256) VALUES(?,?)',
                           (world['source'], old_sha))
    elif binding == 'cross_source':
        other_path = world['path'].with_name('other.txt')
        other_path.write_bytes(world['path'].read_bytes())
        other_source = execute("INSERT INTO source_files(course_id,path,sha256,status) VALUES(?,?,?,'indexed')",
                               (world['course'], str(other_path), world['sha']))
        # The hash matches; the snapshot still belongs to a different source.
        snapshot = execute('INSERT INTO source_snapshots(source_id,sha256) VALUES(?,?)',
                           (other_source, world['sha']))
    text = 'Private unsampled evidence must never silently disappear'
    evidence_id = execute('''INSERT INTO evidence(source_id,snapshot_id,stale,content)
                             VALUES(?,?,0,?)''', (world['source'], snapshot, text))
    assert f'evidence:{evidence_id}' not in world['payload']['sample_refs']
    before = counts()
    with pytest.raises(service().ExtractionConflict) as caught:
        create(world)
    assert text not in str(caught.value) and str(world['path']) not in str(caught.value)
    with closing(db.connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM evidence WHERE source_id=? AND stale=0',
                            (world['source'],)).fetchone()[0] == 11
        assert conn.execute('SELECT COUNT(*) FROM mapping_extraction_runs').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM mapping_extraction_segments').fetchone()[0] == 0
    assert counts() == before and world['calls'] == []
    assert service().list_runs(workspace_key='workpal', mapping_version_id=world['mapping']['id']) == []


BLANK_CONTENT = [
    pytest.param('', id='empty'),
    pytest.param('   ', id='spaces'),
    pytest.param('\t\r\n ', id='tabs-newlines'),
    pytest.param('\u3000\u00a0', id='unicode-whitespace'),
]


@pytest.mark.parametrize('content', BLANK_CONTENT)
@pytest.mark.parametrize('change', ['update', 'insert'])
def test_creation_rejects_any_blank_live_evidence_without_ledger_rows(world, content, change):
    evidence_id = world['evidence'][-1]
    content_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()
    if change == 'insert':
        evidence_id = execute('''INSERT INTO evidence(source_id,snapshot_id,content,content_hash)
                                 VALUES(?,?,?,?)''',
                              (world['source'], world['snapshot'], content, content_hash))
    else:
        execute('UPDATE evidence SET content=?,content_hash=? WHERE id=?',
                (content, content_hash, evidence_id))
    assert f'evidence:{evidence_id}' not in world['payload']['sample_refs']
    with closing(db.connect()) as conn:
        live = conn.execute('SELECT content FROM evidence WHERE source_id=? AND stale=0',
                            (world['source'],)).fetchall()
        assert any(row['content'].strip() for row in live)
        assert any(not row['content'].strip() for row in live)
    before = counts()
    with pytest.raises(service().ExtractionConflict):
        create(world)
    with closing(db.connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM mapping_extraction_runs').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM mapping_extraction_segments').fetchone()[0] == 0
    assert counts() == before and world['calls'] == []
    assert service().list_runs(workspace_key='workpal', mapping_version_id=world['mapping']['id']) == []


@pytest.mark.parametrize('content', BLANK_CONTENT)
def test_creation_replay_rejects_evidence_that_became_blank_without_new_rows(world, content):
    run = create(world)
    with closing(db.connect()) as conn:
        before_runs = [dict(row) for row in conn.execute('SELECT * FROM mapping_extraction_runs ORDER BY id')]
        before_segments = [dict(row) for row in conn.execute('SELECT * FROM mapping_extraction_segments ORDER BY id')]
    before_graph = counts()
    execute('UPDATE evidence SET content=?,content_hash=? WHERE id=?',
            (content, hashlib.sha256(content.encode('utf-8')).hexdigest(), world['evidence'][-1]))
    with pytest.raises(service().ExtractionConflict):
        create(world)
    with closing(db.connect()) as conn:
        assert [dict(row) for row in conn.execute('SELECT * FROM mapping_extraction_runs ORDER BY id')] == before_runs
        assert [dict(row) for row in conn.execute('SELECT * FROM mapping_extraction_segments ORDER BY id')] == before_segments
    assert action('get_run', run)['status'] == 'conflict'
    assert counts() == before_graph and world['calls'] == []


@pytest.mark.parametrize('changes', [
    {'source_ids': []}, {'source_ids': [True]}, {'source_ids': [1.0]}, {'source_ids': '1'},
    {'source_ids': [1, 1]}, {'source_ids': [2**63]}, {'source_ids': [-1]},
    {'mapping_version_id': True}, {'mapping_version_id': 1.0}, {'mapping_hash': 'bad'},
])
def test_strict_creation_inputs(world, changes):
    with pytest.raises(ValueError):
        create(world, **changes)


def test_exact_approved_scope_and_workspace_are_required(world):
    with pytest.raises(service().ExtractionConflict):
        create(world, source_ids=[world['source'] + 1])
    with pytest.raises(service().ExtractionNotFound):
        create(world, workspace_key='studypal')
    with pytest.raises(service().ExtractionConflict):
        create(world, mapping_hash='0' * 64)
    draft = ontology_mappings.create_mapping('workpal', dict(world['payload'], prompt_version='different'))
    with pytest.raises(service().ExtractionConflict):
        create(world, mapping_version_id=draft['id'], mapping_hash=draft['mapping_hash'])


def test_one_step_only_model_budget_and_completed_steps_are_idempotent(world):
    before = counts()
    run = create(world)
    first = action('step_run', run)
    assert first['coverage']['succeeded'] == 1
    assert first['coverage']['pending'] == 11
    args, kwargs = world['calls'][0]
    assert args[0] == 'fast'
    assert kwargs['timeout_seconds'] == 45 and kwargs['max_tokens'] == 4000
    for _ in range(11):
        final = action('step_run', run)
    assert final['status'] == 'completed'
    assert action('step_run', run) == final
    assert len(world['calls']) == 12
    assert counts() == before


@pytest.mark.parametrize('name', ['get_run', 'step_run', 'cancel_run', 'retry_run'])
def test_cross_workspace_and_strict_run_ids(world, name):
    run = create(world)
    with pytest.raises(service().ExtractionNotFound):
        action(name, run, workspace_key='studypal')
    for value in (True, 1.0, 0, -1, '1', 2**63):
        with pytest.raises(ValueError):
            getattr(service(), name)(value, workspace_key='workpal')


def test_read_paths_do_not_write_even_when_stale(world, monkeypatch):
    run = create(world)
    action('step_run', run)
    world['path'].unlink()
    original = db.connect

    def readonly():
        conn = original()
        conn.execute('PRAGMA query_only=ON')
        return conn

    monkeypatch.setattr(db, 'connect', readonly)
    result = action('get_run', run)
    assert result['status'] == 'conflict' and result['conflicts']
    assert service().list_runs(workspace_key='workpal', mapping_version_id=world['mapping']['id'])[0] == result
    with closing(original()) as conn:
        assert conn.execute('SELECT status FROM mapping_extraction_runs').fetchone()[0] != 'conflict'


MUTATIONS = [
    'evidence', 'new_evidence', 'chunk', 'snapshot', 'locator', 'stale', 'index',
    'path', 'file', 'missing', 'mapping', 'profile', 'workspace', 'classification',
]


def mutate(world, kind, monkeypatch):
    if kind == 'evidence':
        execute('UPDATE evidence SET content=content||? WHERE id=?', (' changed', world['evidence'][-1]))
    elif kind == 'new_evidence':
        execute("INSERT INTO evidence(source_id,snapshot_id,content) VALUES(?,?,'new')",
                (world['source'], world['snapshot']))
    elif kind == 'chunk':
        execute("INSERT INTO source_chunks(source_id,position,content) VALUES(?,999,'new')", (world['source'],))
    elif kind == 'snapshot':
        # A different version must respect UNIQUE(source_id, sha256).
        changed_sha = hashlib.sha256(b'synthetic changed snapshot').hexdigest()
        assert changed_sha != world['sha']
        execute('INSERT INTO source_snapshots(source_id,sha256) VALUES(?,?)', (world['source'], changed_sha))
    elif kind == 'locator':
        execute('UPDATE evidence SET locator_end=1000 WHERE id=?', (world['evidence'][-1],))
    elif kind == 'stale':
        execute('UPDATE evidence SET stale=1 WHERE id=?', (world['evidence'][-1],))
    elif kind == 'index':
        execute("UPDATE source_files SET indexed_at='new' WHERE id=?", (world['source'],))
    elif kind == 'path':
        alternate = world['path'].with_name('renamed.txt')
        alternate.write_bytes(world['path'].read_bytes())
        execute('UPDATE source_files SET path=? WHERE id=?', (str(alternate), world['source']))
    elif kind == 'file':
        world['path'].write_text('Private disk changes', encoding='utf-8')
    elif kind == 'missing':
        world['path'].unlink()
    elif kind == 'mapping':
        execute("UPDATE ontology_mapping_versions SET status='superseded' WHERE id=?", (world['mapping']['id'],))
    elif kind == 'profile':
        profiles = copy.deepcopy(domain_profiles.load_profiles())
        profiles['work']['display_name'] += ' changed'
        monkeypatch.setattr(domain_profiles, 'load_profiles', lambda: profiles)
    elif kind == 'workspace':
        execute("UPDATE courses SET workspace_id=(SELECT id FROM workspaces WHERE key='studypal') WHERE id=?",
                (world['course'],))
    else:
        execute("UPDATE source_files SET source_kind='development_doc' WHERE id=?", (world['source'],))


@pytest.mark.parametrize('kind', MUTATIONS)
@pytest.mark.parametrize('when', ['before', 'during'])
def test_full_state_changes_block_output_before_and_after_model(world, monkeypatch, kind, when):
    run = create(world)
    if when == 'before':
        mutate(world, kind, monkeypatch)
    else:
        def change(*args, **kwargs):
            mutate(world, kind, monkeypatch)
            return json.dumps({'candidates': [candidate()], 'relations': []})
        monkeypatch.setattr(llm, 'chat', change)
    with pytest.raises(service().ExtractionConflict) as caught:
        action('step_run', run)
    assert str(world['path']) not in str(caught.value)
    assert 'Private' not in str(caught.value)
    result = action('get_run', run)
    assert result['status'] == 'conflict'
    assert result['candidates'] == result['relations'] == []
    assert all(s['candidates'] == [] for s in result['segments'])
    assert result['coverage']['succeeded'] == 0
    assert world['calls'] == []


@pytest.mark.parametrize('kind', MUTATIONS)
def test_pre_model_revalidation_after_build_messages_persists_conflict_without_calls(world, monkeypatch, kind):
    from app.memory import extraction_output

    run = create(world)
    before = counts()
    original = extraction_output.build_messages
    built = []

    def build_messages(**kwargs):
        messages = original(**kwargs)
        built.append(kwargs['evidence_text'])
        mutate(world, kind, monkeypatch)
        return messages

    monkeypatch.setattr(extraction_output, 'build_messages', build_messages)
    with pytest.raises(service().ExtractionConflict) as caught:
        action('step_run', run)
    assert len(built) == 1
    # Read SQLite directly: a projected stale response alone is insufficient.
    with closing(db.connect()) as conn:
        persisted = conn.execute('SELECT status,error FROM mapping_extraction_runs WHERE id=?',
                                 (run['id'],)).fetchone()
        segments = conn.execute('''SELECT status,claim_token,claimed_at,output_json
                                   FROM mapping_extraction_segments WHERE run_id=?''', (run['id'],)).fetchall()
    assert persisted['status'] == 'conflict' and persisted['error'] == str(caught.value)
    assert len(segments) == run['coverage']['total']
    assert all(row['status'] == 'cancelled' and row['claim_token'] is None
               and row['claimed_at'] is None and row['output_json'] is None for row in segments)
    result = action('get_run', run)
    assert result['status'] == 'conflict'
    assert result['candidates'] == result['relations'] == []
    assert result['coverage']['succeeded'] == 0
    assert counts() == before
    assert world['calls'] == [], 'Changed evidence or contract must be blocked before llm.chat'


@pytest.mark.parametrize('invalidation', ['cancel', 'cancel_retry', 'expired_retry'])
def test_pre_model_revalidation_blocks_cancelled_or_replaced_claim_without_calls(world, monkeypatch, invalidation):
    from app.memory import extraction_output

    store = service()
    clock = [1000.0]
    monkeypatch.setattr(store.time, 'time', lambda: clock[0])
    run = create(world)
    before = counts()
    original = extraction_output.build_messages
    built = []

    def build_messages(**kwargs):
        messages = original(**kwargs)
        built.append(kwargs['evidence_text'])
        if invalidation.startswith('cancel'):
            action('cancel_run', run)
        if invalidation == 'expired_retry':
            clock[0] += store.CLAIM_TIMEOUT_SECONDS
        if invalidation.endswith('retry'):
            action('retry_run', run)
        return messages

    monkeypatch.setattr(extraction_output, 'build_messages', build_messages)
    result = action('step_run', run)
    assert len(built) == 1
    assert result['status'] == ('cancelled' if invalidation == 'cancel' else 'ready')
    assert result['coverage']['running'] == result['coverage']['succeeded'] == 0
    assert result['candidates'] == result['relations'] == []
    assert sum(segment['attempts'] for segment in result['segments']) == 1
    with closing(db.connect()) as conn:
        assert conn.execute('''SELECT COUNT(*) FROM mapping_extraction_segments
                               WHERE run_id=? AND (claim_token IS NOT NULL OR claimed_at IS NOT NULL
                                                   OR output_json IS NOT NULL)''', (run['id'],)).fetchone()[0] == 0
    assert counts() == before
    assert world['calls'] == [], 'Cancelled or replaced claims must not call llm.chat'


def test_missing_disk_source_blocks_creation_without_rows(world):
    world['path'].unlink()
    with pytest.raises(service().ExtractionConflict):
        create(world)
    assert service().list_runs(workspace_key='workpal', mapping_version_id=world['mapping']['id']) == []


def test_cancellation_inside_model_drops_response_and_keeps_success(world, monkeypatch):
    run = create(world)
    action('step_run', run)

    def cancel(*args, **kwargs):
        result = action('cancel_run', run)
        assert result['status'] == 'cancelled'
        assert kwargs['should_cancel']()
        return json.dumps({'candidates': [candidate()], 'relations': []})

    monkeypatch.setattr(llm, 'chat', cancel)
    result = action('step_run', run)
    assert result['status'] == 'cancelled'
    assert result['coverage']['succeeded'] == 1 and result['coverage']['cancelled'] == 11
    assert result['candidates'] == []
    assert action('cancel_run', run) == result
    assert action('step_run', run) == result


def test_concurrent_create_and_claim_never_replay_live_work(world, monkeypatch):
    barrier = Barrier(2)
    def competing(_):
        barrier.wait(timeout=10)
        return create(world)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = list(pool.map(competing, range(2)))
    assert a['id'] == b['id']
    entered, release = Event(), Event()
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return '{"candidates":[],"relations":[]}'
    monkeypatch.setattr(llm, 'chat', blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(action, 'step_run', a)
        try:
            assert entered.wait(10)
            result = action('step_run', a)
            assert result['coverage']['running'] == 1
            assert sum(s['attempts'] for s in result['segments']) == 1
            with pytest.raises(service().ExtractionConflict):
                action('retry_run', a)
        finally:
            release.set()
        assert future.result(timeout=10)['coverage']['succeeded'] == 1


def test_failed_piece_needs_explicit_retry_and_preserves_success(world, monkeypatch):
    run = create(world)
    action('step_run', run)
    def failed(*args, **kwargs):
        raise RuntimeError('private provider details /home/secret sk-fake')
    monkeypatch.setattr(llm, 'chat', failed)
    result = action('step_run', run)
    assert result['status'] == 'partial' and result['coverage']['failed'] == 1
    assert 'private' not in json.dumps(result) and 'sk-fake' not in json.dumps(result)
    model(monkeypatch)
    result = action('step_run', run)
    assert result['coverage']['failed'] == 1 and result['coverage']['succeeded'] == 2
    result = action('retry_run', run)
    assert result['coverage']['failed'] == 0 and result['coverage']['succeeded'] == 2
    result = action('step_run', run)
    assert result['segments'][0]['attempts'] == 1
    assert result['segments'][1]['attempts'] == 2


def test_stale_claim_only_explicit_retry_at_120_seconds_discards_late_response(world, monkeypatch):
    store = service()
    clock = [1000.0]
    monkeypatch.setattr(store.time, 'time', lambda: clock[0])
    run = create(world)
    def expired(*args, **kwargs):
        clock[0] += 119
        with pytest.raises(store.ExtractionConflict):
            action('retry_run', run)
        clock[0] += 1
        assert action('step_run', run)['coverage']['running'] == 1
        retried = action('retry_run', run)
        assert retried['coverage']['running'] == 0 and retried['coverage']['pending'] == 12
        return json.dumps({'candidates': [candidate()], 'relations': []})
    monkeypatch.setattr(llm, 'chat', expired)
    result = action('step_run', run)
    assert result['coverage']['succeeded'] == 0 and result['candidates'] == []
    model(monkeypatch)
    result = action('step_run', run)
    assert result['segments'][0]['attempts'] == 2 and result['coverage']['succeeded'] == 1


def test_aggregate_dedup_and_directed_relations_keep_all_provenance(world, monkeypatch):
    run = create(world)
    model(monkeypatch, [candidate(), candidate('Other', id='b')],
          [{'from_id': 'a', 'to_id': 'b', 'kind': 'supports', 'quote': 'Evidence'}])
    action('step_run', run)
    result = action('step_run', run)
    assert len(result['candidates']) == 2 and len(result['relations']) == 1
    for item in result['candidates'] + result['relations']:
        assert len(item['segment_refs']) == len(item['evidence_refs']) == 2
        assert item['evidence_refs'][0]['source_sha256'] == world['sha']
        assert item['evidence_refs'][0]['mapping_hash'] == world['mapping']['mapping_hash']
        assert item['evidence_refs'][0]['binding_hash']
    assert result['relations'][0]['from_id'] == result['candidates'][0]['id']
    assert result['relations'][0]['to_id'] == result['candidates'][1]['id']
    assert result['conflicts'] == []


@pytest.mark.parametrize('changes', [
    {'kp_type': 'concept'}, {'gist': 'Different'}, {'detail': 'Different'},
    {'conditions': 'Only locally'}, {'valid_from': '2026-01-01'}, {'valid_to': '2027-01-01'},
])
def test_same_normalized_name_different_statements_are_preserved_as_conflicts(world, monkeypatch, changes):
    run = create(world)
    model(monkeypatch, [candidate()])
    action('step_run', run)
    model(monkeypatch, [candidate(' Shared   claim ', **changes)])
    result = action('step_run', run)
    assert len(result['candidates']) == 2
    assert any(c['kind'] == 'same_name_variants' for c in result['conflicts'])


def test_existing_collisions_are_visible_without_publication(world, monkeypatch):
    execute('''INSERT INTO knowledge_candidates(source_id,schema_version,name,kp_type,gist)
               VALUES(?,'test','Shared claim','method','Existing')''', (world['source'],))
    execute("INSERT INTO kp(course_id,name,kp_type,gist) VALUES(?,'Shared claim','method','Existing')",
            (world['course'],))
    before = counts()
    run = create(world)
    model(monkeypatch, [candidate()])
    result = action('step_run', run)
    assert {'existing_candidate', 'existing_kp'} <= {c['kind'] for c in result['conflicts']}
    assert counts() == before


def test_creation_and_success_persistence_roll_back_atomically(world, monkeypatch):
    execute('''CREATE TRIGGER fail_second_piece BEFORE INSERT ON mapping_extraction_segments
               WHEN NEW.evidence_id > 1 BEGIN SELECT RAISE(ABORT,'synthetic'); END''')
    with pytest.raises(sqlite3.IntegrityError):
        create(world)
    with closing(db.connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM mapping_extraction_runs').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM mapping_extraction_segments').fetchone()[0] == 0
    execute('DROP TRIGGER fail_second_piece')
    run = create(world)
    execute('''CREATE TRIGGER fail_result BEFORE UPDATE ON mapping_extraction_segments
               WHEN NEW.status='succeeded' BEGIN SELECT RAISE(ABORT,'synthetic'); END''')
    model(monkeypatch, [candidate()])
    with pytest.raises(sqlite3.IntegrityError):
        action('step_run', run)
    result = action('get_run', run)
    assert result['coverage']['running'] == 1 and result['coverage']['succeeded'] == 0
    assert result['candidates'] == []


def test_statement_normalization_never_collapses_distinct_mathematical_text(world, monkeypatch):
    run = create(world)
    model(monkeypatch, [candidate(detail='X²')])
    action('step_run', run)
    model(monkeypatch, [candidate(detail='X2')])
    result = action('step_run', run)
    assert len(result['candidates']) == 2
    assert any(c['kind'] == 'same_name_variants' for c in result['conflicts'])


def test_same_statement_in_different_chapters_dedups_and_retains_locations(world, monkeypatch):
    run = create(world)
    model(monkeypatch, [candidate(chapter='First')])
    action('step_run', run)
    model(monkeypatch, [candidate(chapter='Second')])
    result = action('step_run', run)
    assert len(result['candidates']) == 1 and result['conflicts'] == []
    assert result['candidates'][0]['chapters'] == ['First', 'Second']
    assert len(result['candidates'][0]['evidence_refs']) == 2


def test_relation_between_duplicate_local_candidates_is_explicit_conflict(world, monkeypatch):
    run = create(world)
    model(monkeypatch, [candidate(), candidate(id='b')],
          [{'from_id': 'a', 'to_id': 'b', 'kind': 'supports', 'quote': 'Evidence'}])
    result = action('step_run', run)
    assert len(result['candidates']) == 1 and result['relations'] == []
    assert any(c['kind'] == 'collapsed_relation' for c in result['conflicts'])
    assert len(result['segments'][0]['candidates']) == 2
    assert len(result['segments'][0]['relations']) == 1


def test_cancel_retry_preserves_success_and_requires_explicit_resumption(world, monkeypatch):
    run = create(world)
    model(monkeypatch, [candidate()])
    action('step_run', run)
    cancelled = action('cancel_run', run)
    assert len(cancelled['candidates']) == 1
    result = action('retry_run', run)
    assert result['coverage']['succeeded'] == 1 and result['coverage']['pending'] == 11
    assert result['segments'][0]['attempts'] == 1
    assert result['segments'][1]['attempts'] == 0
    assert len(result['candidates']) == 1


def test_immutable_host_provenance_validated_json_and_no_replace(world, monkeypatch):
    run = create(world)
    model(monkeypatch, [candidate()])
    result = action('step_run', run)
    with closing(db.connect()) as conn:
        row = conn.execute('SELECT * FROM mapping_extraction_segments WHERE id=?',
                           (result['segments'][0]['id'],)).fetchone()
        output = json.loads(row['output_json'])
        assert output.keys() == {'candidates', 'relations'}
        assert 'binding_hash' not in output['candidates'][0]
        assert row['binding_hash'] == result['segments'][0]['provenance']['binding_hash']
    for sql in (
        "UPDATE mapping_extraction_runs SET source_state_json='changed'",
        "UPDATE mapping_extraction_runs SET mapping_hash='changed'",
        'UPDATE mapping_extraction_segments SET end=end+1',
        "UPDATE mapping_extraction_segments SET binding_hash='changed'",
        "UPDATE mapping_extraction_segments SET output_json='{}' WHERE status='succeeded'",
        'DELETE FROM mapping_extraction_segments',
        'DELETE FROM mapping_extraction_runs',
        'INSERT OR REPLACE INTO mapping_extraction_runs SELECT * FROM mapping_extraction_runs',
        'INSERT OR REPLACE INTO mapping_extraction_segments SELECT * FROM mapping_extraction_segments',
    ):
        with pytest.raises(sqlite3.IntegrityError):
            execute(sql)
    assert action('get_run', run) == result


@pytest.mark.parametrize('raw', ['not json /private/example', '{"candidates":[{}],"relations":[]}',
                                '{"candidates":[],"relations":[],"status":"approved"}'])
def test_real_parser_rejection_is_generic_failed_piece_with_no_partial_output(world, monkeypatch, raw):
    run = create(world)
    monkeypatch.setattr(llm, 'chat', lambda *a, **k: raw)
    result = action('step_run', run)
    assert result['coverage']['failed'] == 1 and result['candidates'] == []
    assert result['segments'][0]['error'] == 'Segment extraction failed; retry explicitly'


@pytest.mark.parametrize('limit', [True, 1.0, '20', 0, -1, 1001])
def test_list_limit_is_strict(world, limit):
    with pytest.raises(ValueError):
        service().list_runs(workspace_key='workpal', mapping_version_id=world['mapping']['id'], limit=limit)
