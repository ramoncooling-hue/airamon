"""T2.1/O-DATA-4: 023 consolidates the legacy inline schema.

The old database fixture is a 0.5.7 state: schema files 001-022 were applied
and db._legacy_upgrade's former inline DDL/backfill ran afterwards. Upgrading
that fixture must apply 023 idempotently, keep a pre-upgrade snapshot, preserve
data and run the two migrated backfills. A build without FTS5 must skip 011 and
degrade to the LIKE fallback instead of failing startup.
"""

import re
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.memory import db, search_index
from app.maintenance import migrations

MIGRATION = '023_legacy_schema_consolidation.sql'
FTS_MIGRATION = '011_search_index.sql'
LEGACY_FILES_THROUGH = 22
DEGRADED = {'sqlite_version': '3.0-test', 'fts5': False, 'trigram': False}
LEGACY_INDEXES = (
    'idx_kp_chapter_key', 'idx_sources_kind', 'idx_courses_workspace',
    'idx_events_workspace', 'idx_conversations_workspace',
)


def _old_legacy_upgrade(conn):
    """The former db._legacy_upgrade body, frozen here as an upgrade fixture."""
    workspace_columns = {
        'courses': {'workspace_id': 'INTEGER REFERENCES workspaces(id) ON DELETE SET NULL'},
        'events': {'workspace_id': 'INTEGER REFERENCES workspaces(id) ON DELETE SET NULL'},
        'conversations': {'workspace_id': 'INTEGER REFERENCES workspaces(id) ON DELETE SET NULL'},
    }
    for table, columns in workspace_columns.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    item_columns = {row[1] for row in conn.execute('PRAGMA table_info(workspace_items)').fetchall()}
    if 'path' not in item_columns:
        conn.execute("ALTER TABLE workspace_items ADD COLUMN path TEXT NOT NULL DEFAULT ''")
    kp_columns = {row[1] for row in conn.execute('PRAGMA table_info(kp)').fetchall()}
    for name, definition in (
        ('chapter_key', "TEXT NOT NULL DEFAULT ''"),
        ('section_key', "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in kp_columns:
            conn.execute(f"ALTER TABLE kp ADD COLUMN {name} {definition}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kp_chapter_key ON kp(course_id, chapter_key)")
    catalog_columns = {row[1] for row in conn.execute('PRAGMA table_info(chapter_catalog)').fetchall()}
    if 'catalog_version' not in catalog_columns:
        conn.execute("ALTER TABLE chapter_catalog ADD COLUMN catalog_version TEXT NOT NULL DEFAULT '1'")
    source_columns = {row[1] for row in conn.execute('PRAGMA table_info(source_files)').fetchall()}
    for name, definition in (
        ('source_kind', "TEXT NOT NULL DEFAULT 'unclassified'"),
        ('source_role', "TEXT NOT NULL DEFAULT 'material'"),
        ('catalog_status', "TEXT NOT NULL DEFAULT 'none'"),
        ('ingest_run_id', "INTEGER REFERENCES ingest_runs(id) ON DELETE SET NULL"),
    ):
        if name not in source_columns:
            conn.execute(f"ALTER TABLE source_files ADD COLUMN {name} {definition}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sources_kind ON source_files(course_id, source_kind, catalog_status)")
    conn.execute("""UPDATE source_files SET source_kind='course_catalog',source_role='catalog',catalog_status='candidate'
                    WHERE source_kind='unclassified' AND id IN
                    (SELECT DISTINCT source_id FROM chapter_catalog WHERE source_id IS NOT NULL)""")
    conn.execute("""UPDATE relation_candidates
                    SET status='rejected', reviewed_at=datetime('now','localtime')
                    WHERE status='pending'
                      AND reason='同一份材料中的已确认节点，待人工审核'""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_courses_workspace ON courses(workspace_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_workspace ON events(workspace_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_workspace ON conversations(workspace_id)")
    event_columns = {row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
    for name, definition in (
        ('source', "TEXT NOT NULL DEFAULT 'local'"),
        ('source_uid', "TEXT NOT NULL DEFAULT ''"),
        ('source_updated_at', "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in event_columns:
            conn.execute(f"ALTER TABLE events ADD COLUMN {name} {definition}")


def _schema_subset(target: Path, through: int = LEGACY_FILES_THROUGH) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    for path in db.SCHEMA_DIR.glob('*.sql'):
        if int(re.match(r'\d+', path.name)[0]) <= through:
            shutil.copy2(path, target / path.name)
    return target


def _maximum_schema_version() -> int:
    return max(int(re.match(r'\d+', path.name)[0])
               for path in db.SCHEMA_DIR.glob('*.sql'))


class Migration023Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = self.root / 'app'
        self.patches = [
            patch.object(db, 'APP_DIR', self.app),
            patch.object(db, 'DB_PATH', self.app / 'migration023.db'),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def _columns(self, table):
        conn = db.connect()
        try:
            return [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
        finally:
            conn.close()

    def _legacy_database(self) -> Path:
        partial = _schema_subset(self.root / 'schema_through_022')
        database = self.root / 'legacy.db'
        migrations.upgrade_database(
            database, partial, legacy_upgrade=_old_legacy_upgrade,
            seed_backfill=db._seed_backfill)
        return database

    def _insert_legacy_rows(self, database: Path) -> None:
        conn = sqlite3.connect(database)
        try:
            with conn:
                conn.execute("INSERT INTO courses(name) VALUES('会计学')")
                course_id = conn.execute(
                    "SELECT id FROM courses WHERE name='会计学'").fetchone()[0]
                conn.execute("INSERT INTO kp(course_id,name) VALUES(?,?)",
                             (course_id, '边际成本'))
                conn.execute("INSERT INTO source_files(course_id,path) VALUES(?,?)",
                             (course_id, '教材.pdf'))
                source_id = conn.execute(
                    'SELECT id FROM source_files').fetchone()[0]
                conn.execute(
                    """INSERT INTO chapter_catalog
                           (course_id,source_id,chapter_key,number)
                       VALUES(?,?,?,?)""", (course_id, source_id, 'ch1', 1))
                conn.execute(
                    """INSERT INTO relation_candidates
                           (source_id,from_name,to_name,reason)
                       VALUES(?,?,?,?)""",
                    (source_id, 'A', 'B', '同一份材料中的已确认节点，待人工审核'))
        finally:
            conn.close()

    def test_capability_probe_reports_sqlite_and_trigram(self):
        capabilities = db.sqlite_capabilities()
        self.assertEqual(capabilities['sqlite_version'], sqlite3.sqlite_version)
        self.assertIsInstance(capabilities['fts5'], bool)
        self.assertIsInstance(capabilities['trigram'], bool)
        # The trigram tokenizer only exists inside an FTS5-capable build.
        self.assertFalse(capabilities['trigram'] and not capabilities['fts5'])

    def test_023_is_marker_guarded_and_consolidates_legacy_schema(self):
        path = db.SCHEMA_DIR / MIGRATION
        self.assertEqual(path.read_text(encoding='utf-8').splitlines()[0].strip(),
                         '-- @idempotent-alter')
        self.assertIn(MIGRATION, db.apply_schema())
        expected_columns = {
            'courses': ['workspace_id'],
            'events': ['workspace_id', 'source', 'source_uid', 'source_updated_at'],
            'conversations': ['workspace_id'],
            'workspace_items': ['path'],
            'kp': ['chapter_key', 'section_key'],
            'source_files': ['source_kind', 'source_role', 'catalog_status', 'ingest_run_id'],
            'chapter_catalog': ['catalog_version'],
        }
        for table, columns in expected_columns.items():
            actual = self._columns(table)
            for column in columns:
                self.assertEqual(actual.count(column), 1, f'{table}.{column}')
        conn = db.connect()
        try:
            versions = {row[0] for row in conn.execute('SELECT version FROM schema_migrations')}
            self.assertIn(MIGRATION, versions)
            self.assertIn(migrations.LEGACY_VERSION, versions)
            for index in LEGACY_INDEXES:
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name=?",
                    (index,)).fetchone()[0], 1, index)
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],
                             _maximum_schema_version())
            capabilities = {row['name']: row['detail'] for row in conn.execute(
                'SELECT name,detail FROM schema_capabilities')}
            self.assertIn(capabilities['fts5'], ('available', 'unavailable'))
            self.assertIn(capabilities['search_fts'], ('fts5', 'skipped'))
        finally:
            conn.close()

    def test_legacy_upgrade_creates_snapshot_and_runs_023_backfills(self):
        legacy = self._legacy_database()
        self._insert_legacy_rows(legacy)
        result = migrations.upgrade_database(
            legacy, db.SCHEMA_DIR, legacy_upgrade=lambda conn: None,
            seed_backfill=db._seed_backfill)
        self.assertIn(MIGRATION, result['applied'])
        self.assertTrue(result['backup'])
        self.assertTrue(Path(result['backup']).is_file())
        conn = sqlite3.connect(legacy)
        try:
            self.assertEqual(
                conn.execute('SELECT source_kind,source_role,catalog_status '
                             'FROM source_files').fetchone(),
                ('course_catalog', 'catalog', 'candidate'))
            status, reviewed_at = conn.execute(
                'SELECT status,reviewed_at FROM relation_candidates').fetchone()
            self.assertEqual(status, 'rejected')
            self.assertTrue(reviewed_at)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM kp').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM courses').fetchone()[0], 1)
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],
                             _maximum_schema_version())
        finally:
            conn.close()

    def test_023_replay_after_record_loss_is_idempotent(self):
        db.apply_schema()
        conn = db.connect()
        try:
            with conn:
                conn.execute("INSERT INTO courses(name) VALUES('会计学')")
                conn.execute(
                    "INSERT INTO kp(course_id,name) "
                    "SELECT id,'边际成本' FROM courses WHERE name='会计学'")
                conn.execute('DELETE FROM schema_migrations WHERE version=?', (MIGRATION,))
        finally:
            conn.close()
        result = migrations.upgrade_database(
            db.DB_PATH, db.SCHEMA_DIR, legacy_upgrade=db._legacy_upgrade,
            seed_backfill=db._seed_backfill)
        self.assertIn(MIGRATION, result['applied'])
        self.assertTrue(result['backup'])
        self.assertTrue(Path(result['backup']).is_file())
        for table, columns in (('courses', ['workspace_id']),
                               ('kp', ['chapter_key', 'section_key']),
                               ('source_files', ['source_kind'])):
            actual = self._columns(table)
            for column in columns:
                self.assertEqual(actual.count(column), 1, f'{table}.{column}')
        conn = db.connect()
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM kp').fetchone()[0], 1)
            for index in LEGACY_INDEXES:
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name=?",
                    (index,)).fetchone()[0], 1, index)
            self.assertEqual(conn.execute(
                'SELECT COUNT(*) FROM schema_capabilities').fetchone()[0], 4)
        finally:
            conn.close()

    def test_fts5_unavailable_build_skips_011_and_degrades_search(self):
        with patch.object(db, 'sqlite_capabilities', lambda: dict(DEGRADED)):
            applied = db.apply_schema()
            self.assertIn(FTS_MIGRATION, applied)
            self.assertEqual(search_index.rebuild(), 0)
            conn = db.connect()
            try:
                self.assertIn(FTS_MIGRATION, {row[0] for row in conn.execute(
                    'SELECT version FROM schema_migrations')})
                self.assertIsNone(conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='search_fts'").fetchone())
                capabilities = {row['name']: row['detail'] for row in conn.execute(
                    'SELECT name,detail FROM schema_capabilities')}
                self.assertEqual(capabilities['fts5'], 'unavailable')
                self.assertEqual(capabilities['search_fts'], 'skipped')
                conn.execute("INSERT INTO courses(name) VALUES('会计学')")
                conn.execute(
                    "INSERT INTO kp(course_id,name,gist) "
                    "SELECT id,'边际成本','每多生产一单位增加的成本' "
                    "FROM courses WHERE name='会计学'")
                conn.commit()
            finally:
                conn.close()
            self.assertEqual(
                [row['title'] for row in search_index.search('边际成本')],
                ['边际成本'])
            counts = db.table_counts()
            self.assertNotIn('search_fts', counts)
            self.assertIn('courses', counts)
            # A second boot on the same degraded build must not back up again.
            db.apply_schema()
            self.assertFalse((self.app / 'backups').exists())
        # search_fts is missing but the capability probe now says FTS5 works:
        # search must still degrade instead of crashing.
        self.assertEqual(
            [row['title'] for row in search_index.search('边际成本')],
            ['边际成本'])

    def test_fts_schema_replays_when_build_gains_fts5(self):
        with patch.object(db, 'sqlite_capabilities', lambda: dict(DEGRADED)):
            db.apply_schema()
        result = migrations.upgrade_database(
            db.DB_PATH, db.SCHEMA_DIR, legacy_upgrade=db._legacy_upgrade,
            seed_backfill=db._seed_backfill)
        self.assertIn(FTS_MIGRATION, result['applied'])
        conn = db.connect()
        try:
            self.assertEqual(conn.execute(
                "SELECT type FROM sqlite_master WHERE name='search_fts'").fetchone()[0],
                'table')
            capabilities = {row['name']: row['detail'] for row in conn.execute(
                'SELECT name,detail FROM schema_capabilities')}
            self.assertEqual(capabilities['fts5'], 'available')
            self.assertEqual(capabilities['search_fts'], 'fts5')
            conn.execute("INSERT INTO courses(name) VALUES('会计学')")
            conn.execute(
                "INSERT INTO kp(course_id,name,gist) "
                "SELECT id,'边际成本','每多生产一单位增加的成本' "
                "FROM courses WHERE name='会计学'")
            conn.commit()
        finally:
            conn.close()
        self.assertGreaterEqual(search_index.rebuild(), 1)
        self.assertEqual(
            [row['title'] for row in search_index.search('边际成本')],
            ['边际成本'])


if __name__ == '__main__':
    unittest.main()
