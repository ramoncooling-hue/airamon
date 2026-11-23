import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.memory import db, repo


class GraphEditingTests(unittest.TestCase):
    def test_update_is_reversible_in_revision_log_and_edges_are_editable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'):
                db.apply_schema()
                conn = db.connect()
                conn.execute("INSERT INTO courses(name) VALUES('测试课程')")
                course_id = conn.execute("SELECT id FROM courses WHERE name='测试课程'").fetchone()[0]
                conn.execute("INSERT INTO kp(course_id,name,gist) VALUES(?,?,?)", (course_id, '节点A', '旧要点'))
                conn.execute("INSERT INTO kp(course_id,name,gist) VALUES(?,?,?)", (course_id, '节点B', '另一个要点'))
                conn.commit()
                ids = [row[0] for row in conn.execute("SELECT id FROM kp ORDER BY id").fetchall()]
                conn.close()

                result = repo.update_kp(ids[0], {'name': '节点A修订', 'gist': '新要点'}, origin='test')
                self.assertEqual(result['changed'], ['name', 'gist'])
                check = db.connect()
                try:
                    revisions = check.execute(
                        "SELECT field,old_value,new_value,origin FROM revisions WHERE kp_id=? ORDER BY id",
                        (ids[0],),
                    ).fetchall()
                finally:
                    check.close()
                self.assertEqual([(r[0], r[1], r[2], r[3]) for r in revisions], [
                    ('name', '节点A', '节点A修订', 'test'),
                    ('gist', '旧要点', '新要点', 'test'),
                ])

                edge = repo.create_edge(ids[0], ids[1], 'related')['edge']
                graph = repo.graph_of('测试课程')
                self.assertEqual(graph['edges'][0]['id'], edge['id'])
                self.assertEqual(repo.delete_edge(edge['id'])['deleted'], True)
                self.assertEqual(repo.graph_of('测试课程')['edges'], [])


if __name__ == '__main__':
    unittest.main()
