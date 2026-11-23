import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agent_runtime.context import ContextCompiler
from app.memory import db, repo


class PersonalMemoryAndNeighborsTests(unittest.TestCase):
    def isolated(self):
        return patch.object(db, 'APP_DIR', self.root / 'app'), patch.object(
            db, 'DB_PATH', self.root / 'app' / 'test.db'
        )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = self.isolated()
        self.patches[0].start()
        self.patches[1].start()
        db.apply_schema()

    def tearDown(self):
        self.patches[1].stop()
        self.patches[0].stop()
        self.tmp.cleanup()

    def test_memory_blocks_are_layered_searchable_and_versioned(self):
        item = repo.create_memory_block('studypal', {
            'layer': 'rule', 'title': '回答先给结论', 'content': '先给结论，再给来源。',
            'source_type': 'manual', 'source_id': 'test:1',
        })
        self.assertEqual(item['version'], 1)
        updated = repo.update_memory_block(item['id'], {'content': '先给结论，再给来源和下一步。'})
        self.assertEqual(updated['memory']['version'], 2)
        self.assertEqual(len(repo.list_memory_revisions(item['id'])), 2)
        hits = repo.search_memory_blocks('来源', workspace='studypal')
        self.assertEqual([hit['id'] for hit in hits], [item['id']])
        archived = repo.archive_memory_block(item['id'])
        self.assertEqual(archived['memory']['status'], 'archived')
        self.assertEqual(repo.search_memory_blocks('来源', workspace='studypal'), [])

    def test_context_exposes_stable_memory_layers_without_unbounded_injection(self):
        repo.create_memory_block('studypal', {
            'layer': 'preference', 'title': '偏好', 'content': '使用中文，回答简洁。',
        })
        context = ContextCompiler().build_context(
            workspace_key='studypal', question='今天怎么复习？'
        )
        self.assertEqual(context['memory_layers'][0]['layer'], 'preference')
        self.assertIn('memory_layers', context['limits'])

    def test_graph_neighbors_are_directional_and_bounded(self):
        conn = db.connect()
        workspace_id = conn.execute("SELECT id FROM workspaces WHERE key='studypal'").fetchone()[0]
        course = conn.execute(
            "INSERT INTO courses(name,workspace_id) VALUES('测试课程',?)", (workspace_id,)
        ).lastrowid
        ids = []
        for name in ('基础', '方法', '案例', '远端'):
            ids.append(conn.execute(
                "INSERT INTO kp(course_id,name,chapter,gist) VALUES(?,?,?,?)",
                (course, name, '第1章', name),
            ).lastrowid)
        conn.executemany(
            "INSERT INTO kp_edges(from_kp,to_kp,kind) VALUES(?,?,?)",
            [(ids[0], ids[1], 'pre'), (ids[1], ids[2], 'supports'), (ids[2], ids[3], 'related')],
        )
        conn.commit()
        conn.close()
        result = repo.kp_neighbors(ids[0], depth=2)
        self.assertEqual([node['name'] for node in result['nodes']], ['方法', '案例'])
        self.assertIn('outgoing', [path['direction'] for path in result['paths']])
        detail = repo.get_kp(ids[1])
        self.assertEqual(detail['relations'][0]['other_name'], '基础')
        self.assertEqual(detail['relations'][0]['direction'], 'incoming')


if __name__ == '__main__':
    unittest.main()
