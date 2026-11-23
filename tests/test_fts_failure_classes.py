"""T2.10/O-REL-4: FTS failures are classified, logged and degrade to LIKE."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.memory import db, search_index

QUERY = '成本核算正文'


class FailingConnection:
    def __init__(self, error):
        self.error = error
        self.closed = False

    def execute(self, *args, **kwargs):
        raise self.error

    def close(self):
        self.closed = True

    def commit(self):
        pass

    def rollback(self):
        pass


class FtsFailureClassTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [
            patch.object(db, 'APP_DIR', self.root / 'app'),
            patch.object(db, 'DB_PATH', self.root / 'app' / 'fts.db'),
        ]
        for item in self.patches:
            item.start()
        db.apply_schema()
        search_index.reset_fts_status()
        conn = db.connect()
        try:
            with conn:
                conn.execute("INSERT INTO courses(name) VALUES('会计学')")
                course_id = conn.execute(
                    "SELECT id FROM courses WHERE name='会计学'").fetchone()[0]
                conn.execute(
                    """INSERT INTO source_files(course_id,path,sha256,status,source_kind)
                       VALUES(?,?,?,'indexed','textbook')""",
                    (course_id, '/tmp/textbook.md', 'hash'))
                conn.execute(
                    'INSERT INTO source_chunks(source_id,position,content) VALUES(1,0,?)',
                    (QUERY + ' 学习区材料',))
        finally:
            conn.close()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def _force_fts(self):
        return patch.object(search_index._db, 'sqlite_capabilities',
                            return_value={'fts5': True, 'trigram': True})

    def _fallback_with_error(self, error, expected_kind):
        real_connection = db.connect()
        fake = FailingConnection(error)
        with self._force_fts(), \
             patch.object(search_index, 'connect', side_effect=[fake, real_connection]):
            with self.assertLogs(search_index.log.name, level='WARNING') as captured:
                rows = search_index.search(QUERY)
        joined = '\n'.join(captured.output)
        self.assertIn('[fts:%s]' % expected_kind, joined)
        self.assertTrue(rows)
        self.assertTrue(all(item['body'] for item in rows))
        self.assertTrue(fake.closed)
        status = search_index.fts_status()
        self.assertEqual(status['last_failure_kind'], expected_kind)
        self.assertEqual(status['last_failure_stage'], 'like')
        self.assertTrue(status['degraded'])
        return joined

    def test_missing_module_is_classified_and_falls_back(self):
        logged = self._fallback_with_error(
            sqlite3.OperationalError('no such module: fts5'), 'missing_module')
        self.assertIn('[fts:missing_module][match]', logged)

    def test_corrupt_index_is_classified_and_falls_back(self):
        self._fallback_with_error(
            sqlite3.DatabaseError('database disk image is malformed'), 'corrupt_index')

    def test_generic_sql_error_is_classified_and_falls_back(self):
        self._fallback_with_error(
            sqlite3.OperationalError('near "MATCH": syntax error'), 'sql_error')

    def test_classify_function_covers_all_three_kinds(self):
        self.assertEqual(search_index.classify_fts_error(
            sqlite3.OperationalError('no such module: fts5')), 'missing_module')
        self.assertEqual(search_index.classify_fts_error(
            sqlite3.OperationalError('no such table: search_fts')), 'missing_module')
        self.assertEqual(search_index.classify_fts_error(
            sqlite3.DatabaseError('database disk image is malformed')), 'corrupt_index')
        self.assertEqual(search_index.classify_fts_error(
            sqlite3.DatabaseError('index corrupt')), 'corrupt_index')
        self.assertEqual(search_index.classify_fts_error(
            sqlite3.OperationalError('database is locked')), 'sql_error')

    def test_missing_capability_uses_like_without_logging_failure(self):
        with patch.object(search_index._db, 'sqlite_capabilities',
                          return_value={'fts5': False, 'trigram': False}):
            rows = search_index.search(QUERY)
            status = search_index.fts_status()
        self.assertTrue(rows)
        self.assertFalse(status['fts5'])
        self.assertTrue(status['degraded'])
        self.assertEqual(status['last_failure_kind'], '')

    def test_rebuild_corrupt_index_returns_zero_and_logs(self):
        fake = FailingConnection(sqlite3.DatabaseError('database disk image is malformed'))
        with self._force_fts(), patch.object(search_index, 'connect', return_value=fake):
            with self.assertLogs(search_index.log.name, level='WARNING') as captured:
                count = search_index.rebuild()
        self.assertEqual(count, 0)
        self.assertIn('[fts:corrupt_index][rebuild]', '\n'.join(captured.output))
        status = search_index.fts_status()
        self.assertEqual(status['last_failure_kind'], 'corrupt_index')
        self.assertEqual(status['last_failure_stage'], 'rebuild')
        self.assertTrue(fake.closed)

    def test_status_shape_and_reset(self):
        status = search_index.fts_status()
        for key in ('fts5', 'degraded', 'last_failure_kind',
                    'last_failure_stage', 'last_failure_detail'):
            self.assertIn(key, status)
        search_index._record_fts_failure(
            'probe', sqlite3.OperationalError('no such module: fts5'))
        self.assertEqual(search_index.fts_status()['last_failure_kind'], 'missing_module')
        search_index.reset_fts_status()
        self.assertEqual(search_index.fts_status()['last_failure_kind'], '')


if __name__ == '__main__':
    unittest.main()
