import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.memory import db, repo


class EventWritebackTests(unittest.TestCase):
    def test_event_crud_writes_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'):
                db.apply_schema()
                event = repo.create_event({'course_name': '会计学', 'title': '交作业',
                                           'category': 'hw', 'date': '2026-10-08', 'note': '线上提交'})
                self.assertEqual(event['title'], '交作业')
                updated = repo.update_event(event['id'], {'title': '交最终作业', 'date': '2026-10-09'})
                self.assertEqual(updated['title'], '交最终作业')
                self.assertTrue(repo.delete_event(event['id']))
                conn = db.connect()
                try:
                    count = conn.execute('SELECT COUNT(*) FROM event_revisions WHERE event_id=?', (event['id'],)).fetchone()[0]
                finally:
                    conn.close()
                self.assertEqual(count, 3)


if __name__ == '__main__':
    unittest.main()
