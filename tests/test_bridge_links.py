import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.memory import db, repo

class BridgeLinkTests(unittest.TestCase):
    def test_bridge_links_resolve_formal_nodes_and_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'):
                db.apply_schema()
                conn = db.connect()
                conn.execute("INSERT INTO courses(name) VALUES('A'),('B')")
                a_course, b_course = [r[0] for r in conn.execute('SELECT id FROM courses ORDER BY id')]
                conn.execute("INSERT INTO kp(course_id,name) VALUES(?,?),(?,?)", (a_course, '节点A', b_course, '节点B'))
                a_id, b_id = [r[0] for r in conn.execute('SELECT id FROM kp ORDER BY id')]
                conn.execute("INSERT INTO kp_identities(kp_id,node_key) VALUES(?,?),(?,?)", (a_id, 'a:node-a', b_id, 'b:node-b'))
                conn.execute("INSERT INTO bridges(course_a,kp_a,course_b,kp_b) VALUES('A','节点A','B','节点B')")
                conn.commit()
                conn.close()
                rows = repo.list_bridges()
                self.assertEqual(rows[0]['status'], 'resolved')
                self.assertEqual(rows[0]['a_node_key'], 'a:node-a')
                self.assertEqual(rows[0]['b_node_key'], 'b:node-b')

if __name__ == '__main__':
    unittest.main()
