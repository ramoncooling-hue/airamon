"""B2/T2.13a: read-only graph/mastery consistency inspection."""

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.api import routes
from app.memory import db, graph_tools, repo, search_index
from scripts import check_graph_health as cli


class GraphHealthConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [
            patch.object(db, 'APP_DIR', self.root / 'app'),
            patch.object(db, 'DB_PATH', self.root / 'app' / 'health.db'),
        ]
        for item in self.patches:
            item.start()
        db.apply_schema()
        conn = db.connect()
        try:
            with conn:
                self.course_id = conn.execute(
                    "INSERT INTO courses(name) VALUES('会计学')").lastrowid
                self.node_a = conn.execute(
                    "INSERT INTO kp(course_id,name,kp_type) VALUES(?,?,'concept')",
                    (self.course_id, '甲')).lastrowid
                self.node_b = conn.execute(
                    "INSERT INTO kp(course_id,name,kp_type) VALUES(?,?,'concept')",
                    (self.course_id, '乙')).lastrowid
                for node_id, name in ((self.node_a, '甲'), (self.node_b, '乙')):
                    conn.execute(
                        'INSERT INTO kp_identities(kp_id,node_key) VALUES(?,?)',
                        (node_id, repo._node_key(self.course_id, name)))
                conn.execute(
                    "INSERT INTO kp_edges(from_kp,to_kp,kind) VALUES(?,?,'related')",
                    (self.node_a, self.node_b))
                for node_id in (self.node_a, self.node_b):
                    conn.execute(
                        """INSERT INTO knowledge_node_meta(kp_id,domain,attributes_json,
                                                          source_refs_json,status)
                           VALUES(?,?, '{}', '[]', 'confirmed')""",
                        (node_id, 'mba'))
                bridge = conn.execute(
                    """INSERT INTO bridges(course_a,kp_a,course_b,kp_b)
                       VALUES('会计学','甲','会计学','乙')""").lastrowid
                conn.execute(
                    """INSERT INTO bridge_links(bridge_id,a_kp_id,b_kp_id,
                                                a_node_key,b_node_key,status)
                       VALUES(?,?,?,?,?,'resolved')""",
                    (bridge, self.node_a, self.node_b,
                     repo._node_key(self.course_id, '甲'),
                     repo._node_key(self.course_id, '乙')))
        finally:
            conn.close()
        search_index.rebuild()
        self.monitor = sqlite3.connect(str(db.DB_PATH))

    def tearDown(self):
        self.monitor.close()
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def _raw(self):
        conn = sqlite3.connect(str(db.DB_PATH))
        conn.execute('PRAGMA foreign_keys=OFF')
        return conn

    def _inject(self):
        raw = self._raw()
        try:
            with raw:
                # ① kp without an existing course.
                raw.execute("INSERT INTO kp(course_id,name,kp_type) VALUES(999,'孤儿','concept')")
                # ② identity missing (丙) / identity drift (甲).
                self.node_c = raw.execute(
                    "INSERT INTO kp(course_id,name,kp_type) VALUES(?,?,'concept')",
                    (self.course_id, '丙')).lastrowid
                raw.execute("UPDATE kp_identities SET node_key='drifted' WHERE kp_id=?",
                            (self.node_a,))
                # ③ bridge: missing kp, unresolved, node_key drift.
                broken = raw.execute(
                    "INSERT INTO bridges(course_a,kp_a,course_b,kp_b) VALUES('会计学','甲','会计学','丙')"
                ).lastrowid
                raw.execute(
                    """INSERT INTO bridge_links(bridge_id,a_kp_id,b_kp_id,a_node_key,
                                                b_node_key,status)
                       VALUES(?,999,?,'','','resolved')""",
                    (broken, self.node_a))
                unresolved_bridge = raw.execute(
                    "INSERT INTO bridges(course_a,kp_a,course_b,kp_b) VALUES('会计学','甲','会计学','乙')"
                ).lastrowid
                raw.execute(
                    """INSERT INTO bridge_links(bridge_id,a_kp_id,b_kp_id,a_node_key,
                                                b_node_key,status)
                       VALUES(?,?,?,'','','unresolved')""",
                    (unresolved_bridge, self.node_a, self.node_b))
                drift_bridge = raw.execute(
                    "INSERT INTO bridges(course_a,kp_a,course_b,kp_b) VALUES('会计学','甲','会计学','丙')"
                ).lastrowid
                raw.execute(
                    """INSERT INTO bridge_links(bridge_id,a_kp_id,b_kp_id,a_node_key,
                                                b_node_key,status)
                       VALUES(?,?,?,'old-key','','resolved')""",
                    (drift_bridge, self.node_a, self.node_c))
                # ④ relation candidate pointing at a missing kp + stale evidence.
                raw.execute(
                    """INSERT INTO relation_candidates(source_id,from_kp,to_kp,from_name,
                                                       to_name,kind,evidence_id,status)
                       VALUES(NULL,999,?,'孤儿','甲','related',999999,'pending')""",
                    (self.node_a,))
                # ⑤ metadata: missing (丙), invalid JSON + stale refs (甲).
                raw.execute("DELETE FROM knowledge_node_meta WHERE kp_id=?", (self.node_c,))
                raw.execute(
                    """UPDATE knowledge_node_meta
                          SET attributes_json='{oops',
                              source_refs_json='[{"evidence_id": 424242}]'
                        WHERE kp_id=?""", (self.node_a,))
                # ⑥ invalid mastery.
                raw.execute("UPDATE kp SET mastery='错档' WHERE id=?", (self.node_a,))
                # ⑦ chapter key unknown + mismatch.
                raw.execute("UPDATE kp SET chapter='第1章',chapter_key='ch-02' WHERE id=?",
                            (self.node_a,))
                # ⑧ soft-deleted kp still referenced by an edge.
                raw.execute(
                    "UPDATE kp SET deleted_at='2026-10-17 00:00:00' WHERE id=?",
                    (self.node_b,))
                # ⑨ FTS drift: drop the indexed kp rows without rebuilding.
                if db.sqlite_capabilities().get('fts5'):
                    raw.execute("DELETE FROM search_fts WHERE kind='kp'")
        finally:
            raw.close()

    def _kinds(self, report):
        return {item['kind']: item for item in report['issues']}

    def test_clean_database_is_ok(self):
        report = graph_tools.check_graph_health('会计学')
        self.assertTrue(report['ok'])
        self.assertEqual(report['issues'], [])
        self.assertEqual(report['totals'], {'errors': 0, 'warnings': 0})
        self.assertIn('checked_at', report)
        self.assertEqual(report['counts']['kp'], 2)
        self.assertEqual(report['counts']['edges'], 1)
        self.assertEqual(report['counts']['bridges'], 1)
        self.assertEqual(report['counts']['search_fts'], 2)
        json.dumps(report)  # structured report is JSON-serialisable

    def test_health_endpoint_keeps_legacy_fields_and_adds_consistency(self):
        report = routes.graph_health('会计学')
        for key in ('course', 'node_count', 'edge_count', 'isolated_nodes',
                    'duplicate_nodes', 'dangling_edges', 'unresolved_bridges',
                    'contract_gaps', 'contract_ok', 'ok'):
            self.assertIn(key, report)
        self.assertIn('consistency', report)
        self.assertTrue(report['consistency_ok'])
        self.assertEqual(report['consistency']['counts']['kp'], 2)

    def test_consistency_report_is_read_only(self):
        baseline = self.monitor.execute('PRAGMA data_version').fetchone()[0]
        graph_tools.health('会计学')
        graph_tools.check_graph_health('会计学')
        routes.graph_health('会计学')
        self.assertEqual(self.monitor.execute('PRAGMA data_version').fetchone()[0], baseline)

    def test_detects_injected_inconsistencies(self):
        self._inject()
        report = graph_tools.check_graph_health('会计学')
        self.assertFalse(report['ok'])
        kinds = self._kinds(report)
        expected = {
            'kp_missing_course': 1,
            'identity_missing': 1,
            'identity_drift': 1,
            'bridge_unresolved': 1,
            'bridge_kp_missing': 1,
            # The injected identity drift for 甲 also stales the clean bridge's
            # cached node_key, so both links that cache 甲 are reported.
            'bridge_node_key_drift': 2,
            'candidate_kp_missing': 1,
            'candidate_stale_evidence': 1,
            'meta_missing': 1,
            'meta_invalid_json': 1,
            'meta_stale_refs': 1,
            'invalid_mastery': 1,
            'chapter_key_unknown': 1,
            'chapter_key_mismatch': 1,
        }
        for kind, count in expected.items():
            self.assertIn(kind, kinds, kind)
            self.assertEqual(kinds[kind]['count'], count, kind)
        self.assertIn('deleted_kp_referenced', kinds)
        self.assertGreaterEqual(kinds['deleted_kp_referenced']['count'], 1)
        if db.sqlite_capabilities().get('fts5'):
            self.assertIn('search_fts_drift', kinds)
            drift = {item['kind']: item for item in kinds['search_fts_drift']['samples']}
            self.assertIn('kp', drift)
            self.assertLess(drift['kp']['actual'], drift['kp']['expected'])
        self.assertGreater(report['totals']['errors'], 0)
        self.assertGreater(report['totals']['warnings'], 0)
        for item in report['issues']:
            self.assertLessEqual(len(item['samples']), 5)
            self.assertIn(item['severity'], ('error', 'warning'))

    def test_empty_database_degrades_without_crash(self):
        with patch.object(db, 'DB_PATH', self.root / 'app' / 'empty.db'):
            (self.root / 'app' / 'empty.db').write_bytes(b'')
            report = graph_tools.check_graph_health()
        self.assertTrue(report['ok'])
        self.assertEqual(report['issues'], [])
        self.assertTrue(any('未初始化' in hint for hint in report['hints']))

    def test_course_missing_is_reported(self):
        report = graph_tools.check_graph_health('不存在的课程')
        self.assertFalse(report['ok'])
        kinds = self._kinds(report)
        self.assertEqual(kinds['course_missing']['count'], 1)
        self.assertEqual(kinds['course_missing']['severity'], 'error')

    def test_degraded_without_fts_does_not_raise(self):
        with patch('app.memory.db.sqlite_capabilities',
                   return_value={'fts5': False, 'trigram': False}):
            report = graph_tools.check_graph_health('会计学')
        self.assertTrue(report['ok'])
        self.assertTrue(any('FTS5' in hint for hint in report['hints']))

    def test_cli_json_markdown_and_strict(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(['--course', '会计学'])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload['ok'])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(['--course', '会计学', '--format', 'markdown']), 0)
        self.assertIn('PASS', out.getvalue())
        self.assertIn('图谱/掌握度一致性巡检', out.getvalue())
        self._inject()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(['--course', '会计学', '--strict']), 1)
        self.assertFalse(json.loads(out.getvalue())['ok'])
        self.assertEqual(cli.main(['--course', '会计学']), 0)

    def test_cli_without_database_is_unavailable(self):
        with patch.object(db, 'DB_PATH', self.root / 'missing' / 'none.db'):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main([])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(out.getvalue())['ok'])


if __name__ == '__main__':
    unittest.main()
