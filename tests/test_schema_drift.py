"""T2.1/O-DATA-4: a pure-schema build and a legacy upgrade converge.

Before 023 the legacy ALTERs, indexes and backfills lived only in
db._legacy_upgrade, so a production database could not be rebuilt from
app/schema. These tests emulate a 0.5.7 database (schema files 001-022 plus
the old inline DDL/backfill) and compare its upgraded structure with a fresh
database built purely from the schema directory.
"""

import re
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.memory import db
from app.maintenance import migrations

LEGACY_FILES_THROUGH = 22


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


def _parse_structure(database: Path) -> dict:
    conn = sqlite3.connect(database)
    try:
        structure = {
            'user_version': conn.execute('PRAGMA user_version').fetchone()[0],
            'migrations': sorted(row[0] for row in conn.execute(
                'SELECT version FROM schema_migrations')),
        }
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        structure['tables'] = tables
        for table in tables:
            structure[f'table:{table}'] = {
                'columns': [tuple(row[1:]) for row in conn.execute(
                    f'PRAGMA table_info("{table}")')],
                'foreign_keys': sorted(tuple(row) for row in conn.execute(
                    f'PRAGMA foreign_key_list("{table}")')),
                'indexes': sorted(tuple(row) for row in conn.execute(
                    f'PRAGMA index_list("{table}")')),
            }
        return structure
    finally:
        conn.close()


class SchemaDriftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = self.root / 'app'
        self.patches = [
            patch.object(db, 'APP_DIR', self.app),
            patch.object(db, 'DB_PATH', self.app / 'fresh.db'),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def _fresh_database(self) -> Path:
        db.apply_schema()
        return db.DB_PATH

    def _legacy_0_5_7_database(self) -> Path:
        partial = _schema_subset(self.root / 'schema_through_022')
        database = self.root / 'legacy.db'
        migrations.upgrade_database(
            database, partial, legacy_upgrade=_old_legacy_upgrade,
            seed_backfill=db._seed_backfill)
        return database

    def test_fresh_build_and_legacy_upgrade_have_identical_structure(self):
        fresh = self._fresh_database()
        legacy = self._legacy_0_5_7_database()
        migrations.upgrade_database(
            legacy, db.SCHEMA_DIR, legacy_upgrade=lambda conn: None,
            seed_backfill=db._seed_backfill)
        self.assertEqual(_parse_structure(fresh), _parse_structure(legacy))

    def test_legacy_fixture_matches_0_5_7_state_before_023(self):
        legacy = self._legacy_0_5_7_database()
        conn = sqlite3.connect(legacy)
        try:
            versions = {row[0] for row in conn.execute('SELECT version FROM schema_migrations')}
            self.assertIn(migrations.LEGACY_VERSION, versions)
            self.assertNotIn('023_legacy_schema_consolidation.sql', versions)
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],
                             LEGACY_FILES_THROUGH)
            columns = {row[1] for row in conn.execute('PRAGMA table_info(courses)')}
            self.assertIn('workspace_id', columns)
        finally:
            conn.close()
        migrations.upgrade_database(
            legacy, db.SCHEMA_DIR, legacy_upgrade=lambda conn: None,
            seed_backfill=db._seed_backfill)
        conn = sqlite3.connect(legacy)
        try:
            versions = {row[0] for row in conn.execute('SELECT version FROM schema_migrations')}
            self.assertIn('023_legacy_schema_consolidation.sql', versions)
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],
                             _maximum_schema_version())
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
