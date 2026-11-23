import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

from app.api import routes
from app.memory import db
from app.ingest import jobs
import time


def raw_request(payload: bytes) -> Request:
    sent = False

    async def receive() -> dict:
        nonlocal sent
        if sent:
            return {'type': 'http.disconnect'}
        sent = True
        return {'type': 'http.request', 'body': payload, 'more_body': False}

    return Request({
        'type': 'http', 'method': 'POST', 'path': '/api/intake/upload',
        'headers': [], 'query_string': b'',
    }, receive)


class SourceClassificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [
            patch.object(db, 'DATA_DIR', self.root),
            patch.object(db, 'APP_DIR', self.root / 'app'),
            patch.object(db, 'DB_PATH', self.root / 'app' / 'test.db'),
            patch.object(routes, 'STUDYPAL_DIR', self.root / 'StudyPal'),
        ]
        for item in self.patches:
            item.start()
        db.apply_schema()
        conn = db.connect()
        conn.execute("INSERT INTO courses(name) VALUES('管理经济学')")
        conn.commit()
        conn.close()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def test_catalog_upload_persists_kind_role_and_candidate_status(self):
        result = asyncio.run(routes.intake_upload(
            raw_request('# 第1章 需求\n1.1 需求曲线\n'.encode()),
            course='管理经济学', filename='课程目录.md',
            source_kind='course_catalog', source_role='', mime='text/markdown',
        ))
        self.assertEqual(result['source_kind'], 'course_catalog')
        self.assertEqual(result['catalog_status'], 'candidate')
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
            self.assertTrue(job['source_id'])
        finally:
            jobs.stop()
            jobs._stop.clear()
            jobs._accepting = True
        conn = db.connect()
        try:
            row = conn.execute(
                'SELECT source_kind,source_role,catalog_status,ingest_run_id FROM source_files'
            ).fetchone()
            self.assertEqual(tuple(row), ('course_catalog', 'catalog', 'candidate', 1))
            run = conn.execute(
                'SELECT source_kind,source_role,status FROM ingest_runs'
            ).fetchone()
            self.assertEqual(tuple(run), ('course_catalog', 'catalog', 'completed'))
        finally:
            conn.close()

    def test_development_doc_is_saved_outside_course_index(self):
        result = asyncio.run(routes.intake_upload(
            raw_request('# 平台规范\n开发文档不属于课程知识。\n'.encode()),
            course='管理经济学', filename='开发规范.md',
            source_kind='development_doc', source_role='', mime='text/markdown',
        ))
        target = Path(result['path'])
        self.assertTrue(target.is_file())
        self.assertEqual(target.parent, (self.root / 'app' / 'incoming' / 'development').resolve())
        self.assertFalse(result['indexed'])
        self.assertEqual(result['status'], '已保存到平台资料区，未进入课程图谱')
        conn = db.connect()
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM source_files').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM ingest_runs').fetchone()[0], 1)
        finally:
            conn.close()

    def test_unknown_kind_is_normalized_to_unclassified(self):
        self.assertEqual(routes.sources.normalize_source_kind('not-a-kind'), 'unclassified')

    def test_invalid_base64_does_not_leave_running_ingest(self):
        with self.assertRaises(HTTPException) as ctx:
            routes.intake(routes.IntakeIn(
                course='管理经济学', filename='坏文件.txt', content_base64='!!!',
            ))
        self.assertEqual(ctx.exception.status_code, 400)
        conn = db.connect()
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM ingest_runs').fetchone()[0], 0)
        finally:
            conn.close()

    def test_oversized_stream_marks_ingest_failed(self):
        request = raw_request(b'x')
        request.scope['headers'] = [(b'content-length', str(200 * 1024 * 1024 + 1).encode())]
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(routes.intake_upload(
                request, course='管理经济学', filename='过大.pdf',
                source_kind='textbook', mime='application/pdf',
            ))
        self.assertEqual(ctx.exception.status_code, 413)
        conn = db.connect()
        try:
            run = conn.execute('SELECT status,error FROM ingest_runs').fetchone()
            self.assertEqual(run['status'], 'failed')
            self.assertIn('200 MB', run['error'])
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
