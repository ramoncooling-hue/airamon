import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.memory import db, repo


class NodeIdentityTests(unittest.TestCase):
    def test_identity_is_stable_and_old_name_becomes_alias_on_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'):
                db.apply_schema()
                conn = db.connect()
                conn.execute("INSERT INTO courses(name) VALUES('测试课程')")
                course_id = conn.execute("SELECT id FROM courses WHERE name='测试课程'").fetchone()[0]
                conn.execute("INSERT INTO kp(course_id,name) VALUES(?,?)", (course_id, '成本核算'))
                kp_id = conn.execute('SELECT id FROM kp').fetchone()[0]
                conn.commit()
                conn.close()
                first = repo.ensure_node_identity(kp_id)
                second = repo.ensure_node_identity(kp_id)
                self.assertEqual(first['node_key'], second['node_key'])
                repo.update_kp(kp_id, {'name': '成本管理'}, origin='test')
                conn = db.connect()
                try:
                    alias = conn.execute('SELECT alias FROM kp_aliases WHERE kp_id=?', (kp_id,)).fetchone()[0]
                finally:
                    conn.close()
                self.assertEqual(alias, '成本核算')


if __name__ == '__main__':
    unittest.main()
