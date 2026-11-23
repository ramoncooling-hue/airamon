"""Upgrade an isolated schema-30 library without changing its personal records."""

import re
import shutil
import sqlite3
from contextlib import closing

from app.maintenance import migrations
from app.memory import db


def test_upgrade_preserves_existing_records_and_replays_idempotently(tmp_path, monkeypatch):
    old_schema = tmp_path / 'old-schema'
    old_schema.mkdir()
    for path in db.SCHEMA_DIR.glob('*.sql'):
        if int(re.match(r'\d+', path.name)[0]) <= 30:
            shutil.copy2(path, old_schema / path.name)
    monkeypatch.setattr(db, 'APP_DIR', tmp_path / 'app')
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'app' / 'test.db')
    migrations.upgrade_database(db.DB_PATH, old_schema, legacy_upgrade=db._legacy_upgrade,
                                seed_backfill=db._seed_backfill)
    with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 30
        ws = conn.execute("SELECT id FROM workspaces WHERE key='workpal'").fetchone()[0]
        cid = conn.execute("INSERT INTO courses(name,workspace_id) VALUES('Work records',?)", (ws,)).lastrowid
        conn.execute("INSERT INTO kp(course_id,name,gist,mastery) VALUES(?,'Personal knowledge','Human edit','熟练')", (cid,))
        before = conn.execute('SELECT * FROM kp').fetchall()
        workspace_before = conn.execute('SELECT * FROM workspaces ORDER BY id').fetchall()
    for _ in range(2):
        db.apply_schema()
        with closing(db.connect()) as conn:
            assert conn.execute('PRAGMA user_version').fetchone()[0] >= 31
            assert [tuple(row) for row in conn.execute('SELECT * FROM kp')] == before
            assert [tuple(row) for row in conn.execute('SELECT * FROM workspaces ORDER BY id')] == workspace_before
            assert conn.execute('SELECT COUNT(*) FROM ontology_mapping_versions').fetchone()[0] == 0
            assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
