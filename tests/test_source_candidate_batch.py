import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.api import routes
from app.memory import db, sources


class SourceCandidateBatchTests(unittest.TestCase):
    def test_batch_confirm_writes_nodes_for_pending_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'):
                db.apply_schema()
                conn = db.connect()
                conn.execute("INSERT INTO courses(name) VALUES('会计学')")
                conn.commit()
                conn.close()
                inbox = root / '会计学' / 'inbox'
                inbox.mkdir(parents=True)
                doc = inbox / 'lesson.md'
                doc.write_text('# 成本\n# 收入\n', encoding='utf-8')
                sources.scan_course('会计学', [inbox])
                candidates = sources.candidates('会计学')
                result = routes.review_source_candidates_batch({
                    'action': 'confirmed',
                    'candidate_ids': [item['id'] for item in candidates],
                })
                self.assertTrue(result['ok'])
                self.assertEqual(result['count'], 2)
                conn = db.connect()
                try:
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM kp').fetchone()[0], 2)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM knowledge_candidates WHERE status='confirmed'").fetchone()[0], 2)
                finally:
                    conn.close()


if __name__ == '__main__':
    unittest.main()
