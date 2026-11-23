import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.memory import db
from app.memory import sources


class SourceSyncTests(unittest.TestCase):
    def test_single_file_job_does_not_mark_neighbor_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'):
                db.apply_schema()
                with db.connect() as conn:
                    conn.execute("INSERT INTO courses(name) VALUES('course')")
                conn.close()
                inbox = root / 'course' / 'inbox'
                inbox.mkdir(parents=True)
                first, second = inbox / 'one.md', inbox / 'two.md'
                first.write_text('# One\nFirst material.')
                second.write_text('# Two\nSecond material.')
                sources.scan_course('course', [inbox])
                first.write_text('# One\nUpdated material.')
                sources.scan_course('course', [inbox], only_path=first)
                self.assertEqual([item['status'] for item in sources.status('course')], ['indexed', 'indexed'])

    def test_scan_indexes_new_and_changed_file_once(self):
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
                doc.write_text('# 成本核算\n边际成本反映新增产量的成本。', encoding='utf-8')
                first = sources.scan_course('会计学', [inbox])
                self.assertEqual(first['indexed'], 1)
                self.assertEqual(sources.scan_course('会计学', [inbox])['unchanged'], 1)
                self.assertEqual(sources.search('边际成本', '会计学')[0]['source'], str(doc.resolve()))
                self.assertEqual(sources.candidates('会计学')[0]['status'], 'pending')
                self.assertTrue((root / '会计学' / 'source' / 'lesson.md').exists())
                self.assertTrue((root / '会计学' / 'notes' / 'lesson.md').exists())
                conn = db.connect()
                try:
                    evidence = conn.execute(
                        "SELECT locator_type,locator_start FROM evidence ORDER BY id"
                    ).fetchall()
                finally:
                    conn.close()
                self.assertIn(('line', 1), [(row[0], row[1]) for row in evidence])
                doc.write_text('# 成本核算\n边际成本提高。', encoding='utf-8')
                self.assertEqual(sources.scan_course('会计学', [inbox])['indexed'], 1)
                self.assertEqual(len(sources.search('边际成本', '会计学')), 1)

    def test_assist_wiki_updates_pending_candidates_with_evidence_links(self):
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
                doc.write_text('# 成本\n边际成本是新增一单位产量的成本。', encoding='utf-8')
                sources.scan_course('会计学', [inbox])
                response = '{"summary":"成本摘要。","concepts":[{"name":"边际成本","kp_type":"concept","chapter":"成本","gist":"新增一单位产量的成本","detail":"","trap":"","evidence_refs":[2]}]}'
                with patch.object(sources.wiki.llm, 'chat', return_value=response):
                    result = sources.assist_wiki('会计学', str(doc.resolve()))
                self.assertEqual(result['status'], 'assisted_draft')
                self.assertEqual(result['candidate_count'], 1)
                conn = db.connect()
                try:
                    candidate = conn.execute("SELECT status,gist FROM knowledge_candidates WHERE name='边际成本'").fetchone()
                    links = conn.execute('SELECT COUNT(*) FROM knowledge_candidate_evidence').fetchone()[0]
                finally:
                    conn.close()
                self.assertEqual(candidate['status'], 'pending')
        self.assertEqual(links, 1)

    def test_large_pdf_is_deferred_without_reading_it_into_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'), \
                 patch.object(sources, 'MAX_PDF_EXTRACTION_BYTES', 10):
                db.apply_schema()
                conn = db.connect()
                conn.execute("INSERT INTO courses(name) VALUES('会计学')")
                conn.commit()
                conn.close()
                inbox = root / '会计学' / 'inbox'
                inbox.mkdir(parents=True)
                pdf = inbox / 'large.pdf'
                pdf.write_bytes(b'01234567890')
                result = sources.scan_course('会计学', [inbox])
                self.assertEqual(result['failed'], 1)
                self.assertEqual(result['files'][0]['status'], 'ocr_required')
                self.assertTrue(pdf.exists())
                self.assertIn('超过本机单次解析保护上限', result['files'][0]['error'])


if __name__ == '__main__':
    unittest.main()
