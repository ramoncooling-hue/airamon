"""T2.9/O-DATA-7: formal graph write channel + audit trail."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.memory import db, graph_tools, sources


class FormalWriteChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [
            patch.object(db, 'APP_DIR', self.root / 'app'),
            patch.object(db, 'DB_PATH', self.root / 'app' / 'channel.db'),
        ]
        for item in self.patches:
            item.start()
        db.apply_schema()

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

    def _course_id(self, name='会计学'):
        conn = db.connect()
        try:
            with conn:
                conn.execute("INSERT INTO courses(name) VALUES(?)", (name,))
            return conn.execute('SELECT id FROM courses WHERE name=?', (name,)).fetchone()[0]
        finally:
            conn.close()

    def test_allowlist_accepts_known_scripts_and_marks_owner_only(self):
        self.assertEqual(set(sources.FORMAL_WRITE_SCRIPTS),
                         {'import_wiki_workpal', 'rebuild_graph_data', 'import_graph'})
        for name, relative in sources.FORMAL_WRITE_SCRIPTS.items():
            self.assertTrue((db.ROOT / relative).is_file())
            channel = sources.authorize_formal_write(name)
            self.assertEqual(channel['script'], name)
            self.assertTrue(channel['owner_only'])

    def test_unknown_or_mismatched_callers_are_rejected(self):
        with self.assertRaises(ValueError):
            sources.authorize_formal_write('random_import_script')
        with self.assertRaises(ValueError):
            sources.authorize_formal_write('')
        with patch.object(sys, 'argv', ['uvicorn']):
            with self.assertRaises(ValueError):
                sources.authorize_formal_write('rebuild_graph_data')
            # The user-triggered API import is the only allow_api caller.
            self.assertEqual(
                sources.authorize_formal_write('import_graph', allow_api=True)['script'],
                'import_graph')

    def test_record_formal_write_appends_ingest_runs_audit_row(self):
        course_id = self._course_id()
        channel = sources.authorize_formal_write('rebuild_graph_data')
        run_id = sources.record_formal_write(
            channel, course_id=course_id, nodes=3, edges=2, detail='{"repaired": 3}')
        row = {}
        conn = db.connect()
        try:
            row = dict(conn.execute('SELECT * FROM ingest_runs WHERE id=?', (run_id,)).fetchone())
        finally:
            conn.close()
        self.assertEqual(row['course_id'], course_id)
        self.assertEqual(row['source_kind'], 'maintenance_import')
        self.assertEqual(row['source_role'], 'owner_only')
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['extractor'], 'script:rebuild_graph_data')
        self.assertEqual(row['artifact_count'], 5)
        self.assertIn('repaired', row['error'])
        self.assertTrue(row['finished_at'])
        self.assertEqual(sources.formal_write_audit(channel)['id'], run_id)
        # No other ingestion rows are polluted by the maintenance audit.
        self.assertEqual(self._scalar(
            "SELECT COUNT(*) FROM ingest_runs WHERE status='running'"), 0)

    def test_record_rejects_unauthorized_channel(self):
        with self.assertRaises(ValueError):
            sources.record_formal_write({'script': 'evil'})
        with self.assertRaises(ValueError):
            sources.record_formal_write(None)
        with self.assertRaises(ValueError):
            sources.formal_write_audit({'script': 'evil'})

    def test_graph_import_writes_audit_row(self):
        self._course_id()
        result = graph_tools.import_graph(
            {'schema': 'cyberpal.graph.v1',
             'nodes': [{'name': '审核节点', 'course_name': '会计学', 'gist': '一句话'}],
             'edges': []},
            course='会计学')
        self.assertEqual(result['nodes_created'], 1)
        audit = sources.formal_write_audit(
            sources.authorize_formal_write('import_graph', allow_api=True))
        self.assertEqual(audit['extractor'], 'script:import_graph')
        self.assertEqual(audit['artifact_count'], 1)
        payload = json.loads(audit['error'])
        self.assertEqual(payload, {'nodes_created': 1, 'edges_added': 0})

    def test_maintenance_scripts_use_the_channel(self):
        import_script = (db.ROOT / 'scripts/import_wiki_workpal.py').read_text(encoding='utf-8')
        rebuild_script = (db.ROOT / 'scripts/rebuild_graph_data.py').read_text(encoding='utf-8')
        for text, name in ((import_script, 'import_wiki_workpal'),
                           (rebuild_script, 'rebuild_graph_data')):
            self.assertIn(f"authorize_formal_write('{name}')", text)
            self.assertIn('record_formal_write(', text)


if __name__ == '__main__':
    unittest.main()
