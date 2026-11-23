import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.memory import db, repo


class RelationCandidateTests(unittest.TestCase):
    def test_confirmed_relation_candidate_creates_formal_edge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'):
                db.apply_schema()
                conn = db.connect()
                conn.execute("INSERT INTO courses(name) VALUES('测试课程')")
                cid = conn.execute('SELECT id FROM courses').fetchone()[0]
                conn.execute("INSERT INTO kp(course_id,name) VALUES(?,?), (?,?)", (cid, '节点A', cid, '节点B'))
                a, b = [row[0] for row in conn.execute('SELECT id FROM kp ORDER BY id')]
                conn.execute("INSERT INTO relation_candidates(from_kp,to_kp,from_name,to_name,kind,reason) VALUES(?,?,?,?,?,?)",
                             (a, b, '节点A', '节点B', 'related', '测试候选'))
                conn.commit()
                candidate_id = conn.execute('SELECT id FROM relation_candidates').fetchone()[0]
                conn.close()
                result = repo.review_relation_candidate(candidate_id, 'confirmed')
                self.assertEqual(result['status'], 'confirmed')
                self.assertEqual(repo.graph_of('测试课程')['edges'][0]['kind'], 'related')


if __name__ == '__main__':
    unittest.main()
