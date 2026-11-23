"""T2.20/O-KB-1: 026 editable knowledge model, legacy kp backfill, 022->026 drill."""

import re
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.maintenance import migrations
from app.memory import db, documents

FILE = '026_editable_knowledge.sql'
LEGACY_DB_VERSION = 22
DOCUMENT_COLUMNS = ('workspace_id', 'course_id', 'kp_id', 'kind', 'title', 'body_md',
                    'status', 'version', 'created_at', 'updated_at')


class EditableKnowledgeMigrationTests(unittest.TestCase):
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

    def _scalar(self, sql, params=()):
        conn = db.connect()
        try:
            return conn.execute(sql, params).fetchone()[0]
        finally:
            conn.close()

    def _columns(self, table):
        conn = db.connect()
        try:
            return [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
        finally:
            conn.close()

    def _raw(self):
        return sqlite3.connect(str(db.DB_PATH))

    def _build_old_022_database(self):
        old_dir = self.root / 'schema-022'
        old_dir.mkdir()
        for path in sorted(db.SCHEMA_DIR.glob('*.sql')):
            if int(re.match(r'\d+', path.name)[0]) <= LEGACY_DB_VERSION:
                shutil.copy2(path, old_dir / path.name)
        migrations.upgrade_database(
            db.DB_PATH, old_dir,
            legacy_upgrade=lambda conn: None, seed_backfill=lambda conn: None)
        conn = self._raw()
        try:
            with conn:
                # 0.5.7 libraries already carry the legacy inline DDL (now
                # consolidated into 023) and the seeded workspaces.
                migrations.execute_sql(
                    conn, (db.SCHEMA_DIR / '023_legacy_schema_consolidation.sql')
                    .read_text(encoding='utf-8'), idempotent_alter=True)
                db._seed_backfill(conn)
                conn.execute("INSERT INTO courses(name) VALUES('会计学')")
                conn.execute(
                    "INSERT INTO kp(course_id,name,gist,detail,trap) "
                    "VALUES(1,'成本核算','边际成本的一句话','边际成本的详细解释','机会成本是易错点')")
                conn.execute("INSERT INTO kp(course_id,name) VALUES(1,'固定成本')")
                # A real 0.5.7 library already had its Pod binding backfilled by
                # the runtime legacy upgrade before it is upgraded to 026.
                conn.execute("UPDATE courses SET workspace_id=("
                             "SELECT id FROM workspaces WHERE key='studypal')")
        finally:
            conn.close()

    def test_fresh_database_builds_026_and_guards_checks(self):
        applied = db.apply_schema()
        self.assertIn(FILE, applied)
        for table in ('knowledge_documents', 'document_revisions', 'tags', 'entity_tags'):
            self.assertEqual(self._scalar(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (table,)), 1)
        for column in DOCUMENT_COLUMNS:
            self.assertIn(column, self._columns('knowledge_documents'))
        self.assertIn('deleted_at', self._columns('kp'))
        self.assertIn('workspace_id', self._columns('courses'))
        self.assertEqual(self._scalar(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' "
            "AND name='idx_knowledge_documents_kp_unique'"), 1)
        self.assertIn(FILE, {row[0] for row in db.connect().execute(
            'SELECT version FROM schema_migrations').fetchall()})

        conn = self._raw()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO knowledge_documents(kind,title) VALUES('bad','标题')")
            conn.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO knowledge_documents(title,status) VALUES('标题','bad')")
            conn.rollback()
            with conn:
                conn.execute("INSERT INTO courses(name) VALUES('课程')")
                conn.execute("INSERT INTO kp(course_id,name) VALUES(1,'知识点')")
                conn.execute("INSERT INTO knowledge_documents(kp_id,title) VALUES(1,'第一篇')")
                conn.execute("INSERT INTO knowledge_documents(title) VALUES('未关联一')")
                conn.execute("INSERT INTO knowledge_documents(title) VALUES('未关联二')")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO knowledge_documents(kp_id,title) VALUES(1,'第二篇')")
            conn.rollback()
        finally:
            conn.close()

    def test_existing_migration_numbers_stay_frozen_and_027_stays_free(self):
        # Allow forward migrations while freezing historical names and numbers.
        files = sorted(db.SCHEMA_DIR.glob('*.sql'))
        numbers = {int(re.match(r'\d+', path.name)[0]) for path in files}
        self.assertIn(26, numbers)
        self.assertIn(28, numbers)
        self.assertIn(29, numbers)
        self.assertIn(30, numbers)
        self.assertNotIn(27, numbers)
        self.assertFalse(any(path.name.startswith('027') for path in files))
        self.assertEqual(
            [path.name for path in files
             if 26 <= int(re.match(r'\d+', path.name)[0]) <= 30],
            [FILE, '028_learning_events.sql', '029_lectures.sql',
             '030_agent_memory_action.sql'])

    def test_old_022_database_upgrades_with_snapshot_and_backfill(self):
        self._build_old_022_database()
        raw = self._raw()
        try:
            self.assertEqual(raw.execute('PRAGMA user_version').fetchone()[0], LEGACY_DB_VERSION)
            self.assertEqual(raw.execute('SELECT COUNT(*) FROM kp').fetchone()[0], 2)
            self.assertEqual(raw.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='knowledge_documents'"
            ).fetchone()[0], 0)
        finally:
            raw.close()

        applied = db.apply_schema()
        self.assertIn(FILE, applied)
        # DoD: the pre-upgrade snapshot exists and still carries the legacy rows.
        backups = list((self.root / 'app' / 'backups').glob('before-upgrade-*.db'))
        self.assertEqual(len(backups), 1)
        snapshot = sqlite3.connect(str(backups[0]))
        try:
            self.assertEqual(snapshot.execute('SELECT COUNT(*) FROM kp').fetchone()[0], 2)
            self.assertEqual(snapshot.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='knowledge_documents'"
            ).fetchone()[0], 0)
        finally:
            snapshot.close()
        self.assertFalse((self.root / 'app' / '.schema-upgrade.json').exists())

        workspace_id = self._scalar("SELECT id FROM workspaces WHERE key='studypal'")
        docs = documents.list(workspace='studypal', status='active')
        self.assertEqual(len(docs), 2)
        by_kp = {doc['kp_id']: doc for doc in docs}
        cost = by_kp[1]
        self.assertEqual(cost['kind'], 'concept')
        self.assertEqual(cost['title'], '成本核算')
        self.assertEqual(cost['course_id'], 1)
        self.assertEqual(cost['workspace_id'], workspace_id)
        self.assertEqual(cost['status'], 'active')
        self.assertEqual(cost['version'], 1)
        self.assertIn('边际成本的一句话', cost['body_md'])
        self.assertIn('## 详解', cost['body_md'])
        self.assertIn('边际成本的详细解释', cost['body_md'])
        self.assertIn('## 易错点', cost['body_md'])
        self.assertIn('机会成本是易错点', cost['body_md'])
        self.assertEqual(by_kp[2]['body_md'], '')
        # Retro documents are readable through the repository layer.
        self.assertEqual(documents.get(cost['id'])['title'], '成本核算')
        self.assertEqual(documents.find_by_kp(2)['title'], '固定成本')
        self.assertEqual(self._scalar('SELECT COUNT(*) FROM document_revisions'), 2)
        self.assertEqual(self._scalar(
            "SELECT COUNT(*) FROM document_revisions WHERE origin='backfill'"), 2)

    def test_reapplying_026_skips_alters_and_backfills_as_ignore(self):
        db.apply_schema()
        conn = db.connect()
        try:
            with conn:
                conn.execute("INSERT INTO courses(name) VALUES('会计学')")
                conn.execute("INSERT INTO kp(course_id,name) VALUES(1,'成本核算')")
                conn.execute("INSERT INTO kp(course_id,name) VALUES(1,'固定成本')")
                conn.execute('DELETE FROM schema_migrations WHERE version=?', (FILE,))
        finally:
            conn.close()
        applied = db.apply_schema()
        self.assertIn(FILE, applied)
        self.assertEqual(self._scalar('SELECT COUNT(*) FROM knowledge_documents'), 2)
        self.assertEqual(self._columns('kp').count('deleted_at'), 1)
        self.assertEqual(self._columns('courses').count('workspace_id'), 1)

        conn = db.connect()
        try:
            with conn:
                conn.execute('DELETE FROM schema_migrations WHERE version=?', (FILE,))
        finally:
            conn.close()
        db.apply_schema()
        self.assertEqual(self._scalar('SELECT COUNT(*) FROM knowledge_documents'), 2)
        self.assertEqual(self._scalar(
            'SELECT COUNT(*) FROM schema_migrations WHERE version=?', (FILE,)), 1)


if __name__ == '__main__':
    unittest.main()
