"""T2.11 material-chain fixes: cancel 500, page progress, short PDF, heartbeat log."""

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from threading import Event
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter

from app.api import routes
from app.ingest import jobs
from app.memory import db, search_index, sources


def _text_pdf_bytes(text='Chapter 1 Demand and marginal cost'):
    content = ('BT /F1 18 Tf 72 720 Td (' + text + ') Tj ET').encode('latin-1')
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] '
        b'/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>',
        b'<< /Length ' + str(len(content)).encode() + b' >>\nstream\n' + content + b'\nendstream',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    ]
    out = bytearray(b'%PDF-1.4\n')
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += str(number).encode() + b' 0 obj\n' + body + b'\nendobj\n'
    xref = len(out)
    out += b'xref\n0 ' + str(len(objects) + 1).encode() + b'\n0000000000 65535 f \n'
    for offset in offsets:
        out += ('%010d 00000 n \n' % offset).encode()
    out += (b'trailer\n<< /Size ' + str(len(objects) + 1).encode() +
            b' /Root 1 0 R >>\nstartxref\n' + str(xref).encode() + b'\n%%EOF\n')
    return bytes(out)


def _write_text_pdf(path, *, pages=1, text='Chapter 1 Demand and marginal cost'):
    reader = PdfReader(io.BytesIO(_text_pdf_bytes(text)))
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_page(reader.pages[0])
    with path.open('wb') as handle:
        writer.write(handle)
    return path


def _write_blank_pdf(path):
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with path.open('wb') as handle:
        writer.write(handle)
    return path


class MaterialChainFixTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for key, value in {'DATA_DIR': self.root, 'APP_DIR': self.root / 'app',
                           'DB_PATH': self.root / 'app' / 'test.db'}.items():
            self.stack.enter_context(patch.object(db, key, value))
        self.stack.enter_context(patch.object(routes, 'STUDYPAL_DIR', self.root / 'StudyPal'))
        for key, value in {'_stop': Event(), '_wake': Event(), '_thread': None,
                           '_lease': None, '_accepting': True}.items():
            self.stack.enter_context(patch.object(jobs, key, value))
        db.apply_schema()
        conn = db.connect()
        try:
            with conn:
                conn.execute("INSERT INTO courses(name) VALUES('材料修复')")
        finally:
            conn.close()
        self.folder = self.root / 'StudyPal' / '材料修复' / 'inbox'
        self.folder.mkdir(parents=True)
        app = FastAPI()
        app.include_router(routes.router)
        app.get('/api/health')(lambda: {'status': 'ok'})
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.addCleanup(jobs.stop)
        search_index.rebuild()

    def material(self, name, content):
        path = self.folder / name
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding='utf-8')
        return path

    def _count(self, sql, params=()):
        conn = db.connect()
        try:
            return conn.execute(sql, params).fetchone()[0]
        finally:
            conn.close()

    def _cancel_while_locked(self, task_id):
        blocker = sqlite3.connect(str(db.DB_PATH), timeout=0.2)
        blocker.execute('BEGIN IMMEDIATE')
        try:
            return self.client.post(f'/api/ingest/jobs/{task_id}/cancel')
        finally:
            blocker.rollback()
            blocker.close()

    def test_cancel_returns_409_while_index_write_lock_is_held(self):
        path = self.material('large.md', '# 成本\n' + '边际成本。\n' * 50)
        task = jobs.enqueue('材料修复', path, self.folder)
        response = self._cancel_while_locked(task['id'])
        self.assertEqual(response.status_code, 409)
        self.assertIn('稍后重试', response.json()['detail'])
        self.assertEqual(response.headers.get('retry-after'), '2')
        # A failed cancel must not change the job state.
        self.assertEqual(jobs.get(task['id'])['status'], 'queued')
        # Once the writer is gone the same cancel succeeds normally.
        retry = self.client.post(f'/api/ingest/jobs/{task["id"]}/cancel')
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.json()['status'], 'cancelled')
        self.assertEqual(self._count(
            "SELECT COUNT(*) FROM ingest_jobs WHERE status='running'"), 0)

    def test_cancel_request_after_indexing_lands_cancelled(self):
        path = self.material('note.md', '# 成本核算\n边际成本反映新增产量的成本。')
        task = jobs.enqueue('材料修复', path, self.folder)
        original_sync = jobs.repo.sync_memory_views

        def late_cancel(course):
            original_sync(course)
            jobs.cancel(task['id'])

        with patch.object(jobs.repo, 'sync_memory_views', side_effect=late_cancel):
            self.assertTrue(jobs.run_one())
        job = jobs.get(task['id'])
        self.assertEqual(job['status'], 'cancelled')
        self.assertEqual(self._count(
            "SELECT COUNT(*) FROM ingest_jobs WHERE status='running'"), 0)
        self.assertEqual(self._count(
            "SELECT status FROM ingest_runs WHERE id=?", (job['ingest_run_id'],)), 'cancelled')

    def test_successful_pdf_keeps_page_progress(self):
        path = self.material('big.pdf', b'')
        _write_text_pdf(path, pages=6,
                        text='Chapter 1 Demand ' + 'Marginal cost is the change in total cost. ' * 20)
        task = jobs.enqueue('材料修复', path, self.folder)
        self.assertTrue(jobs.run_one())
        job = jobs.get(task['id'])
        self.assertNotEqual(job['status'], 'failed')
        self.assertEqual(job['pages_total'], 6)
        self.assertEqual(job['pages_done'], 6)

    def test_short_text_pdf_is_indexed_as_text(self):
        path = self.material('short.pdf', b'')
        _write_text_pdf(path, pages=1, text='Chapter 1 Demand and marginal cost')
        task = jobs.enqueue('材料修复', path, self.folder)
        self.assertTrue(jobs.run_one())
        job = jobs.get(task['id'])
        self.assertNotEqual(job['status'], 'failed')
        self.assertNotIn('扫描件', job['error'] or '')
        self.assertEqual(self._count(
            "SELECT status FROM source_files WHERE path=?", (str(path.resolve()),)), 'indexed')

    def test_blank_text_pdf_still_requires_ocr(self):
        path = self.material('blank.pdf', b'')
        _write_blank_pdf(path)
        task = jobs.enqueue('材料修复', path, self.folder)
        self.assertTrue(jobs.run_one())
        job = jobs.get(task['id'])
        self.assertEqual(job['status'], 'failed')
        self.assertIn('OCR', job['error'])
        self.assertEqual(self._count(
            "SELECT status FROM source_files WHERE path=?", (str(path.resolve()),)), 'ocr_required')

    def test_progress_callback_preserves_last_page_counts(self):
        with patch.object(jobs, '_update') as update:
            callback = jobs._progress_callback(7)
            callback('indexing', 0, 0)
            update.assert_called_once_with(7, stage='indexing')
            update.reset_mock()
            callback('parsing', 3, 6)
            update.assert_called_once_with(7, stage='parsing', pages_done=3, pages_total=6)

    def test_heartbeat_expected_lock_logs_warning_once_not_error(self):
        stop = Event()
        calls = {'count': 0}

        def touch(_task_id):
            calls['count'] += 1
            if calls['count'] >= 3:
                stop.set()
            raise sqlite3.OperationalError('database is locked')

        with patch.object(jobs, 'HEARTBEAT_INTERVAL_SECONDS', 0.01), \
             patch.object(jobs, '_touch_heartbeat', side_effect=touch):
            with self.assertLogs('app.ingest.jobs', level='DEBUG') as captured:
                jobs._heartbeat_loop(1, stop)
        levels = [record.levelname for record in captured.records]
        self.assertIn('WARNING', levels)
        self.assertNotIn('ERROR', levels)
        warnings = [record for record in captured.records if record.levelname == 'WARNING']
        self.assertEqual(len(warnings), 1)
        self.assertIn('long write transaction', warnings[0].getMessage())

    def test_heartbeat_real_error_still_logs_exception(self):
        stop = Event()

        def touch(_task_id):
            stop.set()
            raise RuntimeError('boom')

        with patch.object(jobs, 'HEARTBEAT_INTERVAL_SECONDS', 0.01), \
             patch.object(jobs, '_touch_heartbeat', side_effect=touch):
            with self.assertLogs('app.ingest.jobs', level='ERROR') as captured:
                jobs._heartbeat_loop(1, stop)
        errors = [record for record in captured.records if record.levelname == 'ERROR']
        self.assertTrue(errors)
        self.assertIn('ingest heartbeat update failed', errors[0].getMessage())


if __name__ == '__main__':
    unittest.main()
