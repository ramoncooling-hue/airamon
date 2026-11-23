"""Forward-only action CHECK expansion, exercised only on temporary databases."""

import hashlib
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agent_runtime.actions import ALLOWED_ACTIONS, ActionService
from app.agent_runtime.service import PersonalAgentService
from app.maintenance import migrations
from app.memory import db


FILE = '030_agent_memory_action.sql'


class AgentMemoryActionMigrationTests(unittest.TestCase):
    def test_published_026_bytes_remain_compatible_with_existing_libraries(self):
        self.assertEqual(
            hashlib.sha256((db.SCHEMA_DIR / '026_editable_knowledge.sql').read_bytes()).hexdigest(),
            '6340c9f0dca24c5bf34cb43a76c6c90760ed39c012e881e26c229ca0dbafbaf8',
        )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for active_patch in (
            patch.object(db, 'APP_DIR', self.root / 'app'),
            patch.object(db, 'DB_PATH', self.root / 'app' / 'migration.db'),
        ):
            active_patch.start()
            self.addCleanup(active_patch.stop)

    def _old_029(self):
        schema = self.root / 'schema-029'
        schema.mkdir()
        for path in db.SCHEMA_DIR.glob('*.sql'):
            if int(path.name.split('_', 1)[0].rstrip('b')) <= 29:
                shutil.copy2(path, schema / path.name)
        migrations.upgrade_database(db.DB_PATH, schema)

    def _run(self):
        return PersonalAgentService().create_run(workspace_key='studypal')['id']

    def _insert_action(self, conn, run_id, kind='save_memory_block'):
        return conn.execute(
            'INSERT INTO agent_actions(run_id,workspace_key,action_kind) VALUES(?,?,?)',
            (run_id, 'studypal', kind)).lastrowid

    def _indexes(self, conn):
        return {row['name']: row['sql'] for row in conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name='agent_actions'")}

    def test_new_database_accepts_all_known_actions_and_rejects_unknown_actions(self):
        applied = db.apply_schema()
        self.assertIn(FILE, applied)
        run_id = self._run()
        conn = db.connect()
        try:
            with conn:
                for kind in sorted(ALLOWED_ACTIONS):
                    self._insert_action(conn, run_id, kind)
            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_action(conn, run_id, 'unknown_action')
            conn.rollback()
            with self.assertRaises(ValueError):
                ActionService().propose(workspace_key='studypal', run_id=run_id,
                                        action_kind='unknown_action')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM agent_actions').fetchone()[0],
                             len(ALLOWED_ACTIONS))
            latest = max(int(path.name.split('_', 1)[0].rstrip('b'))
                         for path in db.SCHEMA_DIR.glob('*.sql'))
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], latest)
            self.assertEqual(conn.execute('PRAGMA foreign_keys').fetchone()[0], 1)
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
        finally:
            conn.close()

    def test_old_029_upgrade_preserves_rows_sequence_leases_fk_and_indexes(self):
        self._old_029()
        conn = sqlite3.connect(db.DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 29)
            run_id = conn.execute(
                "INSERT INTO agent_runs(workspace_key,agent_key) VALUES('studypal','laoba')").lastrowid
            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_action(conn, run_id)
            with conn:
                for action_id, status in enumerate(
                    ('proposed', 'approved', 'rejected', 'applied', 'failed', 'cancelled'), 41
                ):
                    conn.execute(
                        '''INSERT INTO agent_actions
                           (id,run_id,workspace_key,action_kind,target_type,target_id,
                            proposal_json,before_json,after_json,status,error,created_at,
                            decided_at,applied_at,approved_at,attempt,lease_expires_at)
                           VALUES(?,?,'studypal','update_mastery','kp','17',
                                  '{"mastery":"read"}','{"value":1}','{"value":2}',?,
                                  'retained error','2026-10-01','2026-10-02',?,?,3,?)''',
                        (action_id, run_id, status,
                         '2026-10-03' if status == 'applied' else '',
                         '2026-10-02' if status == 'approved' else None,
                         '2099-01-01' if status == 'approved' else None))
                conn.execute(
                    "INSERT INTO agent_actions(id,run_id,workspace_key,action_kind) "
                    "VALUES(9000,?,'studypal','create_event')", (run_id,))
                conn.execute('DELETE FROM agent_actions WHERE id=9000')
            before = [dict(row) for row in conn.execute('SELECT * FROM agent_actions ORDER BY id')]
            indexes = self._indexes(conn)
            foreign_keys = [tuple(row) for row in conn.execute('PRAGMA foreign_key_list(agent_actions)')]
        finally:
            conn.close()

        self.assertIn(FILE, db.apply_schema())
        conn = db.connect()
        try:
            self.assertEqual([dict(row) for row in conn.execute('SELECT * FROM agent_actions ORDER BY id')], before)
            self.assertEqual(self._indexes(conn), indexes)
            self.assertEqual([tuple(row) for row in conn.execute('PRAGMA foreign_key_list(agent_actions)')],
                             foreign_keys)
            self.assertEqual(conn.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='agent_actions'").fetchone()[0], 9000)
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_action(conn, run_id + 1)
            conn.rollback()
            with conn:
                self.assertEqual(self._insert_action(conn, run_id), 9001)
            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_action(conn, run_id, 'unknown_action')
            conn.rollback()
            with conn:
                conn.execute('DELETE FROM agent_runs WHERE id=?', (run_id,))
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM agent_actions').fetchone()[0], 0)
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
        finally:
            conn.close()

    def test_empty_old_table_keeps_deleted_high_id_sequence(self):
        self._old_029()
        conn = sqlite3.connect(db.DB_PATH)
        try:
            with conn:
                run_id = conn.execute(
                    "INSERT INTO agent_runs(workspace_key,agent_key) VALUES('studypal','laoba')"
                ).lastrowid
                conn.execute(
                    "INSERT INTO agent_actions(id,run_id,workspace_key,action_kind) "
                    "VALUES(9900,?,'studypal','create_event')", (run_id,))
                conn.execute('DELETE FROM agent_actions')
        finally:
            conn.close()
        db.apply_schema()
        conn = db.connect()
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM agent_actions').fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='agent_actions'").fetchone()[0], 9900)
            with conn:
                self.assertEqual(self._insert_action(conn, run_id), 9901)
        finally:
            conn.close()

    def test_replay_preserves_memory_action_data_and_deleted_high_id_sequence(self):
        db.apply_schema()
        run_id = self._run()
        conn = db.connect()
        try:
            with conn:
                action_id = self._insert_action(conn, run_id)
                conn.execute(
                    "UPDATE agent_actions SET status='approved',approved_at='2026-10-22',"
                    "attempt=4,lease_expires_at='2099-01-01' WHERE id=?", (action_id,))
                conn.execute(
                    "INSERT INTO agent_actions(id,run_id,workspace_key,action_kind) "
                    "VALUES(9500,?,'studypal','save_memory_block')", (run_id,))
                conn.execute('DELETE FROM agent_actions WHERE id=9500')
            before = dict(conn.execute('SELECT * FROM agent_actions').fetchone())
            indexes = self._indexes(conn)
        finally:
            conn.close()

        for force_replay in (False, True, True):
            if force_replay:
                conn = db.connect()
                try:
                    with conn:
                        conn.execute('DELETE FROM schema_migrations WHERE version=?', (FILE,))
                finally:
                    conn.close()
            db.apply_schema()
            conn = db.connect()
            try:
                self.assertEqual(dict(conn.execute('SELECT * FROM agent_actions').fetchone()), before)
                self.assertEqual(self._indexes(conn), indexes)
                self.assertEqual(conn.execute(
                    "SELECT seq FROM sqlite_sequence WHERE name='agent_actions'").fetchone()[0], 9500)
                self.assertEqual(conn.execute(
                    'SELECT COUNT(*) FROM schema_migrations WHERE version=?', (FILE,)).fetchone()[0], 1)
                self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
                self.assertEqual(conn.execute(
                    "SELECT name FROM sqlite_master WHERE name LIKE 'agent_actions_030%'"
                ).fetchall(), [])
            finally:
                conn.close()
        conn = db.connect()
        try:
            with conn:
                self.assertEqual(self._insert_action(conn, run_id), 9501)
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
