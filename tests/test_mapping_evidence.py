"""Mapping approval binds live evidence in the requested workspace."""

import copy
import hashlib
import importlib
from contextlib import closing

import pytest

from app.memory import db


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'APP_DIR', tmp_path)
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'test.db')
    db.apply_schema()
    with closing(db.connect()) as conn, conn:
        ws = conn.execute("SELECT id FROM workspaces WHERE key='workpal'").fetchone()[0]
        course = conn.execute("INSERT INTO courses(name,workspace_id) VALUES('Work source',?)", (ws,)).lastrowid
        sha = 'a' * 64
        source = conn.execute("INSERT INTO source_files(course_id,path,sha256,status) VALUES(?,'source.md',?,'indexed')", (course, sha)).lastrowid
        snapshot = conn.execute('INSERT INTO source_snapshots(source_id,sha256) VALUES(?,?)', (source, sha)).lastrowid
        evidence = conn.execute('''INSERT INTO evidence(source_id,snapshot_id,locator_type,locator_start,
                                  locator_end,content,content_hash) VALUES(?,?,'line',1,2,'Original claim',?)''',
                                (source, snapshot, hashlib.sha256(b'Original claim').hexdigest())).lastrowid
    return {'source': source, 'snapshot': snapshot, 'evidence': evidence, 'course': course}


def module():
    return importlib.import_module('app.memory.mapping_evidence')


def capture(world):
    with closing(db.connect()) as conn:
        return module().capture_bindings(conn, 'workpal', [f"evidence:{world['evidence']}"])


def test_binding_is_repeatable_has_no_source_text_or_path_and_verifies(world):
    bindings = capture(world)
    assert bindings == capture(world)
    assert bindings[0]['source_id'] == world['source']
    assert bindings[0]['snapshot_id'] == world['snapshot']
    assert bindings[0]['evidence_id'] == world['evidence']
    assert 'Original claim' not in str(bindings)
    assert 'source.md' not in str(bindings)
    with closing(db.connect()) as conn:
        module().verify_bindings(conn, 'workpal', bindings)


@pytest.mark.parametrize('refs', [[], ['sample-1'], ['evidence:0'], ['evidence:01'], ['evidence:-1'],
                                 ['evidence:1', 'evidence:1'], [True], 'evidence:1', ['evidence:99999']])
def test_unresolvable_sample_references_rejected(world, refs):
    with closing(db.connect()) as conn, pytest.raises(ValueError):
        module().capture_bindings(conn, 'workpal', refs)


def test_other_workspace_or_inactive_workspace_rejected(world):
    with closing(db.connect()) as conn:
        with pytest.raises(ValueError):
            module().capture_bindings(conn, 'studypal', [f"evidence:{world['evidence']}"])
        conn.execute("UPDATE workspaces SET status='archived' WHERE key='workpal'")
        with pytest.raises(ValueError):
            module().capture_bindings(conn, 'workpal', [f"evidence:{world['evidence']}"])


@pytest.mark.parametrize('sql,args', [
    ("UPDATE evidence SET stale=1", ()),
    ("UPDATE evidence SET content='Changed claim'", ()),
    ("UPDATE evidence SET locator_start=99", ()),
    ("UPDATE evidence SET locator_type='page'", ()),
    ("UPDATE evidence SET snapshot_id=NULL", ()),
    ("UPDATE source_files SET status='failed'", ()),
    ("UPDATE source_files SET sha256=?", ('b' * 64,)),
    ("UPDATE source_snapshots SET sha256=?", ('b' * 64,)),
    ("UPDATE courses SET workspace_id=(SELECT id FROM workspaces WHERE key='studypal')", ()),
])
def test_changed_bindings_cannot_be_approved(world, sql, args):
    bindings = capture(world)
    with closing(db.connect()) as conn:
        conn.execute(sql, args)
        with pytest.raises(ValueError):
            module().verify_bindings(conn, 'workpal', bindings)


def test_binding_payload_tampering_is_rejected(world):
    bindings = capture(world)
    bad = copy.deepcopy(bindings)
    bad[0]['evidence_sha256'] = 'f' * 64
    with closing(db.connect()) as conn, pytest.raises(ValueError):
        module().verify_bindings(conn, 'workpal', bad)


def test_empty_content_or_missing_snapshot_cannot_be_captured(world):
    with closing(db.connect()) as conn:
        conn.execute("UPDATE evidence SET content=' '")
        with pytest.raises(ValueError):
            module().capture_bindings(conn, 'workpal', [f"evidence:{world['evidence']}"])


def test_binding_reads_do_not_mutate_database(world):
    with closing(db.connect()) as monitor:
        before = monitor.execute('PRAGMA data_version').fetchone()[0]
        bindings = capture(world)
        with closing(db.connect()) as conn:
            module().verify_bindings(conn, 'workpal', bindings)
        assert monitor.execute('PRAGMA data_version').fetchone()[0] == before


@pytest.mark.parametrize('value', [[], [None], [{}], [{'evidence_id': True}], 'bindings'])
def test_malformed_saved_bindings_fail_closed(world, value):
    with closing(db.connect()) as conn, pytest.raises(ValueError):
        module().verify_bindings(conn, 'workpal', value)


def test_same_text_from_other_source_snapshot_cannot_be_rebound(world):
    with closing(db.connect()) as conn:
        other_source = conn.execute("INSERT INTO source_files(course_id,path,sha256,status) VALUES(?,'other.md',?,'indexed')",
                                    (world['course'], 'a' * 64)).lastrowid
        snapshot = conn.execute('INSERT INTO source_snapshots(source_id,sha256) VALUES(?,?)',
                                (other_source, 'a' * 64)).lastrowid
        conn.execute('UPDATE evidence SET snapshot_id=? WHERE id=?', (snapshot, world['evidence']))
        with pytest.raises(ValueError):
            module().capture_bindings(conn, 'workpal', [f"evidence:{world['evidence']}"])
