import base64
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

from app.api import routes
from app.memory import db
from app.memory import sources
from app.ingest import jobs
import time


def streaming_request(chunks: list[bytes]) -> Request:
    messages = iter([
        {'type': 'http.request', 'body': chunk, 'more_body': index < len(chunks) - 1}
        for index, chunk in enumerate(chunks)
    ])

    async def receive() -> dict:
        try:
            return next(messages)
        except StopIteration:
            return {'type': 'http.disconnect'}

    return Request({
        'type': 'http', 'method': 'POST', 'path': '/api/intake/upload',
        'headers': [], 'query_string': b'',
    }, receive)


class CourseMaterialSurfaceTests(unittest.TestCase):
    def test_streaming_upload_writes_raw_file_and_indexes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'DATA_DIR', root), \
                 patch.object(db, 'APP_DIR', root / 'app'), \
                 patch.object(db, 'DB_PATH', root / 'app' / 'test.db'), \
                 patch.object(routes, 'STUDYPAL_DIR', root / 'StudyPal'):
                db.apply_schema()
                conn = db.connect()
                conn.execute("INSERT INTO courses(name) VALUES('管理经济学')")
                conn.commit()
                conn.close()
                payload = ('# 第1章\n边际成本来自新增一单位产量的成本。\n' * 2000).encode('utf-8')
                result = asyncio.run(routes.intake_upload(
                    streaming_request([payload[:1000], payload[1000:]]),
                    course='管理经济学', filename='教材.md', mime='text/markdown',
                ))
                target = Path(result['path'])
                self.assertTrue(target.exists())
                self.assertEqual(target.read_bytes(), payload)
                self.assertFalse(result['indexed'])
                self.assertEqual(jobs.get(result['task_id'])['status'], 'queued')
                try:
                    jobs.start()
                    self.assertEqual(jobs.get(result['task_id'])['status'], 'interrupted')
                    jobs.retry(result['task_id'])
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        job = jobs.get(result['task_id'])
                        if job['status'] in jobs.TERMINAL:
                            break
                        time.sleep(.02)
                    self.assertEqual(job['status'], 'waiting_review')
                    indexed = sources.status('管理经济学')[0]
                    self.assertEqual(indexed['status'], 'indexed')
                    self.assertEqual(indexed['path'], str(target.resolve()))
                    self.assertTrue(sources.search('边际成本', '管理经济学'))
                finally:
                    jobs.stop()
                    jobs._stop.clear()
                    jobs._accepting = True
                self.assertFalse(list(target.parent.glob('.*.upload')))

    def test_streaming_upload_rejects_oversize_without_partial_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'DATA_DIR', root), \
                 patch.object(db, 'APP_DIR', root / 'app'), \
                 patch.object(db, 'DB_PATH', root / 'app' / 'test.db'), \
                 patch.object(routes, 'STUDYPAL_DIR', root / 'StudyPal'), \
                 patch.object(sources, 'MAX_BYTES', 10):
                db.apply_schema()
                conn = db.connect()
                conn.execute("INSERT INTO courses(name) VALUES('管理经济学')")
                conn.commit()
                conn.close()
                with self.assertRaises(HTTPException) as error:
                    asyncio.run(routes.intake_upload(
                        streaming_request([b'0123456789', b'X']),
                        course='管理经济学', filename='教材.md', mime='text/markdown',
                    ))
                self.assertEqual(error.exception.status_code, 413)
                inbox = root / 'StudyPal' / '管理经济学' / 'inbox'
                self.assertTrue(not inbox.exists() or not list(inbox.iterdir()))

    def test_course_status_exposes_absolute_storage_and_artifact_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'DATA_DIR', root), \
                 patch.object(db, 'APP_DIR', root / 'app'), \
                 patch.object(db, 'DB_PATH', root / 'app' / 'test.db'), \
                 patch.object(routes, 'STUDYPAL_DIR', root / 'StudyPal'):
                db.apply_schema()
                conn = db.connect()
                conn.execute("INSERT INTO courses(name) VALUES('会计学')")
                conn.commit()
                conn.close()
                payload = base64.b64encode('# 成本\n边际成本是新增一单位产量的成本。'.encode()).decode()
                result = routes.intake(routes.IntakeIn(
                    course='会计学', filename='lesson.md', content_base64=payload,
                    mime='text/markdown',
                ))
                status = routes.source_status('会计学')
                self.assertEqual(result['path'], str((root / 'StudyPal' / '会计学' / 'inbox' / 'lesson.md').resolve()))
                self.assertEqual(status['data_dir'], str(root.resolve()))
                self.assertEqual(status['course_dir'], str((root / 'StudyPal' / '会计学').resolve()))
                self.assertEqual(status['inbox_dir'], str((root / 'StudyPal' / '会计学' / 'inbox').resolve()))
                material = status['materials'][0]
                self.assertEqual(material['original_path'], result['path'])
                self.assertTrue(material['artifacts']['note']['exists'])
                self.assertTrue(material['artifacts']['dictionary']['exists'])

    def test_confirmed_material_candidate_is_shared_by_graph_and_mastery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'DATA_DIR', root), \
                 patch.object(db, 'APP_DIR', root / 'app'), \
                 patch.object(db, 'DB_PATH', root / 'app' / 'test.db'), \
                 patch.object(routes, 'STUDYPAL_DIR', root / 'StudyPal'):
                db.apply_schema()
                conn = db.connect()
                conn.execute("INSERT INTO courses(name) VALUES('管理经济学')")
                conn.commit()
                conn.close()
                payload = base64.b64encode(
                    '# 第1章 需求\n边际成本是新增一单位产量带来的成本变化。\n'.encode()
                ).decode()
                routes.intake(routes.IntakeIn(
                    course='管理经济学', filename='lesson.md', content_base64=payload,
                    mime='text/markdown', source_kind='class_note',
                ))
                pending = routes.source_status('管理经济学')['candidates']
                self.assertTrue(pending)
                confirmed = routes.confirm_candidate(pending[0]['id'])
                self.assertEqual(confirmed['status'], 'confirmed')
                graph = routes.graph('管理经济学')
                self.assertIn(confirmed['target_kp_id'], {node['id'] for node in graph['nodes']})
                matrix = routes.mastery('管理经济学')
                self.assertGreaterEqual(sum(row['total'] for row in matrix['rows']), 1)
                conn = db.connect()
                try:
                    candidate = conn.execute(
                        'SELECT status FROM knowledge_candidates WHERE id=?',
                        (pending[0]['id'],),
                    ).fetchone()
                    evidence_count = conn.execute(
                        'SELECT COUNT(*) FROM kp_evidence WHERE kp_id=?',
                        (confirmed['target_kp_id'],),
                    ).fetchone()[0]
                finally:
                    conn.close()
                self.assertEqual(candidate['status'], 'confirmed')
                self.assertGreaterEqual(evidence_count, 1)

    def test_runtime_path_info_and_reveal_are_scoped_to_data_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root / 'StudyPal' / '会计学' / 'inbox' / 'lesson.md'
            target.parent.mkdir(parents=True)
            target.write_text('材料', encoding='utf-8')
            with patch.object(db, 'DATA_DIR', root), \
                 patch('app.api.routes.platform.system', return_value='Darwin'), \
                 patch('app.api.routes.subprocess.Popen') as popen:
                info = routes.runtime_info()
                self.assertEqual(info['data_dir'], str(root))
                self.assertTrue(info['writable'])
                result = routes.runtime_reveal(routes.RevealIn(path=str(target)))
                self.assertTrue(result['ok'])
                popen.assert_called_once_with(['open', '-R', str(target)])
                with self.assertRaises(routes.HTTPException):
                    routes.runtime_reveal(routes.RevealIn(path='/tmp/outside.txt'))

    def test_assisted_draft_can_be_deleted_without_deleting_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'DATA_DIR', root), \
                 patch.object(db, 'APP_DIR', root / 'app'), \
                 patch.object(db, 'DB_PATH', root / 'app' / 'test.db'), \
                 patch.object(routes, 'STUDYPAL_DIR', root / 'StudyPal'):
                db.apply_schema()
                conn = db.connect()
                conn.execute("INSERT INTO courses(name) VALUES('会计学')")
                conn.commit()
                conn.close()
                payload = base64.b64encode('# 成本\n边际成本。'.encode()).decode()
                result = routes.intake(routes.IntakeIn(
                    course='会计学', filename='lesson.md', content_base64=payload,
                    mime='text/markdown',
                ))
                source = Path(result['path'])
                note = source.parent.parent / 'notes' / 'lesson.md'
                dictionary = source.parent.parent / 'dictionary' / 'lesson.json'
                note.write_text('draft', encoding='utf-8')
                dictionary.write_text('{}', encoding='utf-8')
                conn = db.connect()
                conn.execute("UPDATE knowledge_notes SET status='assisted_draft' WHERE path=?", (str(note),))
                conn.commit()
                conn.close()
                result = routes.delete_source_draft(routes.SourceDraftDeleteIn(course='会计学', path=str(source)))
                self.assertTrue(result['ok'])
                self.assertTrue(source.exists())
                self.assertFalse(note.exists())
                self.assertFalse(dictionary.exists())
                self.assertEqual(routes.source_status('会计学')['files'][0]['status'], 'indexed')


if __name__ == '__main__':
    unittest.main()
