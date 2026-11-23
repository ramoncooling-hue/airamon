import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.memory import db, repo


class WorkGraphTests(unittest.TestCase):
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

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def test_non_textbook_workspace_gets_topic_clusters_without_catalog(self):
        repo.create_course('AI工作系统', workspace='workpal')
        conn = db.connect()
        course_id = conn.execute("SELECT id FROM courses WHERE name='AI工作系统'").fetchone()['id']
        ids = []
        for chapter, name in [('AI原生组织协议', 'L1人机协作协议'), ('超级个体与团队', 'AI杠杆')]:
            ids.append(conn.execute(
                'INSERT INTO kp(course_id,name,chapter,gist,kp_type) VALUES(?,?,?,?,?)',
                (course_id, name, chapter, name, 'concept'),
            ).lastrowid)
        conn.execute('INSERT INTO kp_edges(from_kp,to_kp,kind) VALUES(?,?,?)', (ids[0], ids[1], 'supports'))
        conn.commit()
        conn.close()

        graph = repo.graph_of('AI工作系统')
        self.assertEqual([chapter['title'] for chapter in graph['chapters']], ['AI原生组织协议', '超级个体与团队'])
        self.assertEqual(graph['chapter_counts'], {
            graph['chapters'][0]['key']: 1,
            graph['chapters'][1]['key']: 1,
        })
        self.assertEqual(len(graph['edges']), 1)


if __name__ == '__main__':
    unittest.main()
