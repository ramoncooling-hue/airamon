"""Graph import and traversal must remain inside their declared scope."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.memory import db, graph_tools, repo


class GraphScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [
            patch.object(db, 'APP_DIR', root / 'app'),
            patch.object(db, 'DB_PATH', root / 'app' / 'test.db'),
        ]
        for item in self.patches:
            item.start()
        db.apply_schema()
        repo.create_course('学习课')
        repo.create_course('工作项目', workspace='workpal')
        self.study_course = self._course_id('学习课')
        self.work_course = self._course_id('工作项目')

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def _course_id(self, name):
        conn = db.connect()
        try:
            return int(conn.execute('SELECT id FROM courses WHERE name=?', (name,)).fetchone()['id'])
        finally:
            conn.close()

    def _kp(self, course_id, name):
        conn = db.connect()
        try:
            with conn:
                cur = conn.execute('INSERT INTO kp(course_id,name) VALUES(?,?)', (course_id, name))
            return int(cur.lastrowid)
        finally:
            conn.close()

    def _counts(self):
        conn = db.connect()
        try:
            return (
                conn.execute('SELECT COUNT(*) FROM kp').fetchone()[0],
                conn.execute('SELECT COUNT(*) FROM kp_edges').fetchone()[0],
            )
        finally:
            conn.close()

    def test_import_rejects_an_unmapped_integer_that_is_a_local_node_id(self):
        private_id = self._kp(self.work_course, '工作区私有节点')
        before = self._counts()
        payload = {
            'schema': 'cyberpal.graph.v1',
            'nodes': [{'id': 'source', 'name': '导入节点', 'course_name': '学习课'}],
            # A raw database ID must never be accepted as an external endpoint.
            'edges': [{'from_kp': 'source', 'to_kp': private_id, 'kind': 'related'}],
        }

        with self.assertRaisesRegex(ValueError, '端点必须属于本次导入'):
            graph_tools.import_graph(payload)

        self.assertEqual(self._counts(), before, 'invalid imports must roll back their nodes too')

    def test_import_rejects_edges_between_workspaces(self):
        before = self._counts()
        payload = {
            'schema': 'cyberpal.graph.v1',
            'nodes': [
                {'id': 101, 'name': '学习节点', 'course_name': '学习课'},
                {'id': 202, 'name': '工作节点', 'course_name': '工作项目'},
            ],
            'edges': [{'from_kp': 101, 'to_kp': 202, 'kind': 'related'}],
        }

        with self.assertRaisesRegex(ValueError, '同一工作区'):
            graph_tools.import_graph(payload)

        self.assertEqual(self._counts(), before)

    def test_import_rejects_cross_course_edges_even_inside_one_workspace(self):
        repo.create_course('另一门学习课')
        before = self._counts()
        payload = {
            'schema': 'cyberpal.graph.v1',
            'nodes': [
                {'id': 'a', 'name': '概念甲', 'course_name': '学习课'},
                {'id': 'b', 'name': '概念乙', 'course_name': '另一门学习课'},
            ],
            'edges': [{'from_kp': 'a', 'to_kp': 'b', 'kind': 'related'}],
        }

        with self.assertRaisesRegex(ValueError, '同一课程'):
            graph_tools.import_graph(payload)

        self.assertEqual(self._counts(), before)

    def test_import_requires_both_edge_endpoints_to_be_mapped(self):
        existing_id = self._kp(self.study_course, '已存在节点')
        before = self._counts()
        payload = {
            'schema': 'cyberpal.graph.v1',
            'nodes': [{'id': 'new', 'name': '新节点', 'course_name': '学习课'}],
            'edges': [{'from_kp': 'new', 'to_kp': existing_id, 'kind': 'related'}],
        }

        with self.assertRaisesRegex(ValueError, '端点必须属于本次导入'):
            graph_tools.import_graph(payload)

        self.assertEqual(self._counts(), before)

    def test_same_course_import_is_idempotent(self):
        payload = {
            'schema': 'cyberpal.graph.v1',
            'nodes': [
                {'id': 'a', 'name': '概念甲', 'course_name': '学习课'},
                {'id': 'b', 'name': '概念乙', 'course_name': '学习课'},
            ],
            'edges': [{'from_kp': 'a', 'to_kp': 'b', 'kind': 'related'}],
        }

        first = graph_tools.import_graph(payload)
        second = graph_tools.import_graph(payload)

        self.assertEqual((first['nodes_created'], first['edges_added']), (2, 1))
        self.assertEqual((second['nodes_created'], second['edges_added']), (0, 0))

    def test_neighbor_traversal_does_not_leak_cross_workspace_edges(self):
        study_id = self._kp(self.study_course, '学习区根节点')
        work_id = self._kp(self.work_course, '工作区私有节点')
        conn = db.connect()
        try:
            # Simulate a legacy or corrupted cross-workspace edge already in the DB.
            with conn:
                conn.execute(
                    "INSERT INTO kp_edges(from_kp,to_kp,kind) VALUES(?,?,'related')",
                    (study_id, work_id),
                )
        finally:
            conn.close()

        result = repo.kp_neighbors(study_id)

        self.assertEqual(result['nodes'], [])
        self.assertEqual(result['paths'], [])

    def test_knowledge_card_relations_do_not_leak_cross_workspace_edges(self):
        study_id = self._kp(self.study_course, '学习区知识点')
        work_id = self._kp(self.work_course, '工作区私有关系点')
        conn = db.connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO kp_edges(from_kp,to_kp,kind) VALUES(?,?,'related')",
                    (study_id, work_id),
                )
        finally:
            conn.close()

        card = repo.get_kp(study_id)

        self.assertEqual(card['relations'], [])
        self.assertEqual(card['related'], [])


if __name__ == '__main__':
    unittest.main()
