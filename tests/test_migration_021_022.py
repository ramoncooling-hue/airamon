"""T1.4/T1.6: 021/022 migrations are additive and marker-guarded."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.memory import db
from app.maintenance.migrations import execute_sql


LEASE_FILE = '021_action_leases.sql'
SNAPSHOT_FILE = '022_source_snapshots.sql'


class LeaseSnapshotMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [
            patch.object(db, 'APP_DIR', self.root / 'app'),
            patch.object(db, 'DB_PATH', self.root / 'app' / 'migrations.db'),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def _schema_text(self, name):
        return (db.SCHEMA_DIR / name).read_text(encoding='utf-8')

    def _table_columns(self, table):
        conn = db.connect()
        try:
            return [row[1] for row in conn.execute('PRAGMA table_info("' + table + '")')]
        finally:
            conn.close()

    def _scalar(self, sql, params=()):
        conn = db.connect()
        try:
            return conn.execute(sql, params).fetchone()[0]
        finally:
            conn.close()

    def test_schema_files_are_marker_guarded_and_apply_cleanly(self):
        for name in (LEASE_FILE, SNAPSHOT_FILE):
            first_line = self._schema_text(name).splitlines()[0].strip()
            self.assertEqual(first_line, '-- @idempotent-alter')
        applied = db.apply_schema()
        self.assertIn(LEASE_FILE, applied)
        self.assertIn(SNAPSHOT_FILE, applied)
        self.assertIn('approved_at', self._table_columns('agent_actions'))
        self.assertIn('attempt', self._table_columns('agent_actions'))
        self.assertIn('lease_expires_at', self._table_columns('agent_actions'))
        self.assertIn('snapshot_id', self._table_columns('evidence'))
        self.assertIn('stale', self._table_columns('evidence'))
        self.assertIn('snapshot_id', self._table_columns('source_chunks'))
        self.assertIn('stale', self._table_columns('source_chunks'))
        self.assertIn('snapshot_id', self._table_columns('knowledge_candidates'))
        self.assertIn('superseded_by', self._table_columns('knowledge_candidates'))
        self.assertIn('superseded_at', self._table_columns('candidate_reviews'))
        self.assertEqual(self._scalar(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='source_snapshots'"), 1)
        for index in ('idx_agent_actions_lease', 'idx_source_snapshots_source',
                      'idx_evidence_source_stale', 'idx_chunks_source_stale',
                      'idx_knowledge_candidates_snapshot'):
            self.assertEqual(self._scalar(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name=?", (index,)), 1)
        versions = {row[0] for row in db.connect().execute(
            'SELECT version FROM schema_migrations').fetchall()}
        self.assertTrue({LEASE_FILE, SNAPSHOT_FILE} <= versions)

    def test_reapplying_marker_migrations_keeps_existing_data(self):
        db.apply_schema()
        conn = db.connect()
        try:
            with conn:
                conn.execute("INSERT INTO courses(name) VALUES('会计学')")
                conn.execute(
                    'DELETE FROM schema_migrations WHERE version IN (?,?)',
                    (LEASE_FILE, SNAPSHOT_FILE))
        finally:
            conn.close()
        db.apply_schema()
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM courses WHERE name='会计学'"), 1)
        self.assertEqual(self._scalar(
            'SELECT COUNT(*) FROM schema_migrations WHERE version IN (?,?)',
            (LEASE_FILE, SNAPSHOT_FILE)), 2)
        # Each column exists exactly once even though the ALTER statements
        # were executed against a database that already carried them.
        for table, column in (('agent_actions', 'approved_at'),
                              ('agent_actions', 'attempt'),
                              ('agent_actions', 'lease_expires_at'),
                              ('evidence', 'stale'),
                              ('source_chunks', 'stale')):
            self.assertEqual(self._table_columns(table).count(column), 1)
        self.assertEqual(self._scalar(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_agent_actions_lease'"), 1)

    def test_marker_skips_existing_alter_but_plain_sql_still_fails(self):
        self.root.joinpath('app').mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.root / 'app' / 'probe.db'))
        try:
            with conn:
                conn.execute('CREATE TABLE probe(id INTEGER PRIMARY KEY)')
            marker = '-- @idempotent-alter\nALTER TABLE probe ADD COLUMN flag TEXT;'
            with conn:
                execute_sql(conn, marker, idempotent_alter=True)
            with conn:
                conn.execute('INSERT INTO probe(id) VALUES(1)')
                execute_sql(conn, marker, idempotent_alter=True)
            self.assertEqual([row[1] for row in conn.execute('PRAGMA table_info(probe)')].count('flag'), 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM probe').fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                with conn:
                    execute_sql(conn, 'ALTER TABLE probe ADD COLUMN flag TEXT;')
            with conn:
                execute_sql(conn, '-- @idempotent-alter\nCREATE TABLE IF NOT EXISTS probe2(id INTEGER);',
                            idempotent_alter=True)
            with conn:
                execute_sql(conn, '-- @idempotent-alter\nCREATE TABLE IF NOT EXISTS probe2(id INTEGER);',
                            idempotent_alter=True)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='probe2'").fetchone()[0], 1)
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
