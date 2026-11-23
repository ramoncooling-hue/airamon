"""T2.11 材料链路验收：投放/队列/取消/恢复/去重/目录/清单/大文件。

只读产品代码，全部数据在 tmp_path。第 6 组两个用例原用于把复核缺口
（短文本 PDF 误判扫描件、成功 PDF 页进度被重置）固化为 xfail 文档；缺口修复后
已转为常规回归断言，防止回退。
"""

import io
import json
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from threading import Event
from unittest.mock import patch

import docx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfReader, PdfWriter

from app.api import routes
from app.ingest import jobs
from app.memory import db, repo, sources


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


def _write_image_docx(path, *, workdir):
    image = Path(workdir) / 'blank.png'
    Image.new('RGB', (64, 64), 'white').save(image)
    document = docx.Document()
    document.add_picture(str(image))
    document.save(path)
    return path


class MaterialChainAcceptanceTests(unittest.TestCase):
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
        with conn:
            conn.execute("INSERT INTO courses(name) VALUES('材料验收')")
            self.course_id = conn.execute(
                "SELECT id FROM courses WHERE name='材料验收'").fetchone()[0]
        conn.close()
        self.folder = self.root / 'StudyPal' / '材料验收' / 'inbox'
        self.folder.mkdir(parents=True)

    # ── helpers ──────────────────────────────────────────────────────────

    def material(self, name, content):
        path = self.folder / name
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding='utf-8')
        return path

    def scan_and_run_all(self):
        scanned = jobs.scan('材料验收', [self.folder])
        while jobs.run_one():
            pass
        return scanned

    def _count(self, sql, params=()):
        conn = db.connect()
        try:
            return conn.execute(sql, params).fetchone()[0]
        finally:
            conn.close()

    def assert_no_running_left(self):
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM ingest_jobs WHERE status='running'"), 0)

    # ── 1) 代表材料类型 → 队列 → 终态 + 清单字段 ─────────────────────────

    def test_representative_material_types_reach_terminal_states(self):
        # Type1/Helvetica PDF 只能用 latin-1 文本；PDF 分支只验"可提取文本不被误判"。
        pdf_text = ('Chapter 1 Demand and learning objectives ' +
                    'Marginal cost is the change in total cost. ' * 40)
        self.material('note.md', '# 需求\n需求曲线向下倾斜。')
        self.material('note.txt', '边际成本 line\n')
        self._write_docx(self.material('text.docx', b''))
        _write_image_docx(self.material('image.docx', b''), workdir=self.root)
        _write_text_pdf(self.material('textbook.pdf', b''), pages=6, text=pdf_text)

        scanned = self.scan_and_run_all()
        self.assertEqual(scanned['queued'], 5)
        self.assertEqual(scanned['failed'], 0)
        self.assert_no_running_left()

        by_name = {Path(job['path']).name: job for job in jobs.list_jobs(course='材料验收')}
        self.assertEqual(len(by_name), 5)
        for name, job in by_name.items():
            self.assertIn(job['status'], jobs.TERMINAL, name)
        for name in ('note.md', 'note.txt', 'text.docx', 'textbook.pdf'):
            self.assertNotEqual(by_name[name]['status'], 'failed', name)
            self.assertEqual(by_name[name]['error'], '')
        self.assertIn('OCR', by_name['image.docx']['error'])

        inventory = {item['name']: item for item in sources.inventory('材料验收')}
        for name in ('note.md', 'text.docx', 'textbook.pdf'):
            self.assertEqual(inventory[name]['status'], 'indexed', name)
            self.assertGreater(inventory[name]['chunks'], 0, name)
            self.assertGreaterEqual(inventory[name]['versions'], 1, name)
            self.assertTrue(inventory[name]['artifacts']['note']['exists'], name)
        self.assertEqual(inventory['image.docx']['status'], 'ocr_required')
        self.assertIn('OCR', inventory['image.docx']['error'])
        self.assertEqual(inventory['image.docx']['chunks'], 0)

    def _write_docx(self, path):
        document = docx.Document()
        document.add_heading('成本', 1)
        document.add_paragraph('边际成本是新增一单位产量的成本。' * 30)
        document.save(path)
        return path

    # ── 2) 大文件：200 页 PDF 在预算内完成 ──────────────────────────────

    def test_big_200_page_pdf_indexes_within_budget(self):
        path = _write_text_pdf(self.material('big.pdf', b''), pages=200)
        self.assertLess(path.stat().st_size, sources.MAX_PDF_EXTRACTION_BYTES)
        task = jobs.enqueue('材料验收', path, self.folder)
        started = time.monotonic()
        jobs.run_one()
        elapsed = time.monotonic() - started
        job = jobs.get(task['id'])
        self.assertIn(job['status'], jobs.TERMINAL)
        self.assertNotEqual(job['status'], 'failed', job['error'])
        self.assertLess(elapsed, 20.0, '200 页 PDF 解析不应长时间占用单 worker')
        self.assert_no_running_left()
        conn = db.connect()
        try:
            counts = conn.execute(
                'SELECT (SELECT COUNT(*) FROM source_chunks WHERE source_id=? AND stale=0),'
                ' (SELECT COUNT(*) FROM evidence WHERE source_id=? AND stale=0)',
                (job['source_id'], job['source_id'])).fetchone()
        finally:
            conn.close()
        self.assertGreater(counts[0], 0)
        self.assertEqual(counts[1], 200)

    # ── 3) 取消 queued / running ────────────────────────────────────────

    def test_cancel_queued_job_never_extracts_and_can_retry(self):
        task = jobs.enqueue('材料验收', self.material('queued.md', '# 排队\n内容'),
                            self.folder)
        with patch.object(sources, 'extract_structured') as extract:
            cancelled = jobs.cancel(task['id'])
            self.assertEqual(cancelled['status'], 'cancelled')
            self.assertFalse(jobs.run_one())
            extract.assert_not_called()
        retried = jobs.retry(task['id'])
        self.assertEqual(retried['status'], 'queued')
        jobs.run_one()
        self.assertIn(jobs.get(task['id'])['status'], jobs.TERMINAL)
        self.assertEqual(jobs.get(task['id'])['retry_count'], 1)

    def test_cancel_running_job_takes_effect_at_next_checkpoint(self):
        self.material('running.md', '# 运行\n内容')
        entered, release = Event(), Event()
        original = sources.extract_structured

        def blocking(path):
            entered.set()
            release.wait(5)
            return original(path)

        result = {}

        def worker():
            try:
                jobs.run_one()
            except BaseException as exc:  # pragma: no cover - surfaced in assertion
                result['error'] = exc

        with patch.object(sources, 'extract_structured', side_effect=blocking):
            task = jobs.enqueue('材料验收', self.folder / 'running.md', self.folder)
            thread = threading.Thread(target=worker, daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                cancelled = jobs.cancel(task['id'])
                self.assertEqual(cancelled['status'], 'running')
                self.assertEqual(cancelled['cancel_requested'], 1)
                release.set()
                thread.join(5)
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertNotIn('error', result)
        self.assertEqual(jobs.get(task['id'])['status'], 'cancelled')
        self.assert_no_running_left()

    # ── 4) 中断恢复 / 重试 / 去重 ────────────────────────────────────────

    def test_stale_running_job_is_reconciled_then_retryable(self):
        task = jobs.enqueue('材料验收', self.material('stale.md', '# 中断\n内容'),
                            self.folder)
        conn = db.connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE ingest_jobs SET status='running',stage='extracting',"
                    "updated_at=datetime('now','localtime','-120 seconds') WHERE id=?",
                    (task['id'],))
        finally:
            conn.close()
        self.assertEqual(jobs.reconcile_stale_runs(), 1)
        interrupted = jobs.get(task['id'])
        self.assertEqual(interrupted['status'], 'interrupted')
        self.assertTrue(interrupted['error'])
        self.assertEqual(jobs.retry(task['id'])['status'], 'queued')
        jobs.run_one()
        self.assertIn(jobs.get(task['id'])['status'], jobs.TERMINAL)
        self.assert_no_running_left()

    def test_same_digest_returns_canonical_task_and_indexes_once(self):
        first = self.material('one.md', '# 成本\n边际成本。')
        second = self.material('two.md', '# 成本\n边际成本。')
        task = jobs.enqueue('材料验收', first, self.folder)
        duplicate = jobs.enqueue('材料验收', second, self.folder)
        self.assertEqual(task['id'], duplicate['id'])
        self.assertTrue(duplicate['deduplicated'])
        jobs.run_one()
        self.assertFalse(jobs.run_one())
        self.assertEqual(self._count(
            "SELECT COUNT(*) FROM ingest_jobs WHERE course_id=?", (self.course_id,)), 1)

    # ── 5) 目录选择：candidate 不导航 / active 导航 / 切换不删 kp ────────

    def test_catalog_selection_controls_navigation_and_keeps_kp(self):
        first = self.material(
            'catalog-a.md',
            '# 教材 A\n第1章 需求 学习目的\n1.1 需求曲线\n第2章 供给 学习目的\n2.1 供给曲线\n')
        jobs.enqueue('材料验收', first, self.folder, source_kind='course_catalog')
        jobs.run_one()
        first_source = next(item for item in sources.status('材料验收')
                            if item['path'] == str(first.resolve()))
        self.assertEqual(first_source['status'], 'indexed')
        self.assertEqual(first_source['catalog_status'], 'candidate')
        self.assertEqual(repo.graph_of('材料验收')['chapters'], [])
        self.assertEqual(repo.graph_of('材料验收')['catalog_status'], 'missing')

        conn = db.connect()
        try:
            with conn:
                conn.execute("INSERT INTO kp(course_id,name) VALUES(?,?)",
                             (self.course_id, '用户已有知识点'))
        finally:
            conn.close()
        kp_before = self._count('SELECT COUNT(*) FROM kp WHERE course_id=?',
                                (self.course_id,))

        selected = repo.select_catalog_source('材料验收', first_source['id'])
        self.assertEqual(selected['status'], 'active')
        graph = repo.graph_of('材料验收')
        self.assertEqual([chapter['key'] for chapter in graph['chapters']],
                         ['ch-01', 'ch-02'])
        self.assertEqual(graph['catalog_status'], 'ready')
        self.assertEqual(self._count('SELECT COUNT(*) FROM kp WHERE course_id=?',
                                     (self.course_id,)), kp_before)

        second = self.material('catalog-b.md', '# 教材 B\n第1章 成本 学习目的\n1.1 成本曲线\n')
        jobs.enqueue('材料验收', second, self.folder, source_kind='course_catalog')
        jobs.run_one()
        rows = {item['path']: item for item in sources.status('材料验收')}
        self.assertEqual(rows[str(second.resolve())]['catalog_status'], 'candidate')
        second_source = rows[str(second.resolve())]
        repo.select_catalog_source('材料验收', second_source['id'])
        rows = {item['path']: item for item in sources.status('材料验收')}
        self.assertEqual(rows[str(first.resolve())]['catalog_status'], 'historical')
        self.assertEqual(rows[str(second.resolve())]['catalog_status'], 'active')
        self.assertEqual([chapter['key'] for chapter in
                          repo.graph_of('材料验收')['chapters']], ['ch-01'])
        self.assertEqual(self._count('SELECT COUNT(*) FROM kp WHERE course_id=?',
                                     (self.course_id,)), kp_before)


# ── 6) 复核缺口回归（原 xfail，修复后转为常规断言）───────────────────────


def _isolated_env(monkeypatch, tmp_path):
    for key, value in {'DATA_DIR': tmp_path, 'APP_DIR': tmp_path / 'app',
                       'DB_PATH': tmp_path / 'app' / 'test.db'}.items():
        monkeypatch.setattr(db, key, value)
    for key, value in {'_stop': Event(), '_wake': Event(), '_thread': None,
                       '_lease': None, '_accepting': True}.items():
        monkeypatch.setattr(jobs, key, value)
    db.apply_schema()
    conn = db.connect()
    with conn:
        conn.execute("INSERT INTO courses(name) VALUES('缺口')")
    conn.close()
    folder = tmp_path / 'StudyPal' / '缺口' / 'inbox'
    folder.mkdir(parents=True)
    return folder


def test_short_text_pdf_is_not_scanned_image(monkeypatch, tmp_path):
    folder = _isolated_env(monkeypatch, tmp_path)
    path = _write_text_pdf(folder / 'short.pdf', pages=1,
                           text='Chapter 1 Demand and marginal cost')
    task = jobs.enqueue('缺口', path, folder)
    jobs.run_one()
    job = jobs.get(task['id'])
    assert job['status'] == 'succeeded'
    assert '可能是扫描件' not in job['error']


def test_successful_pdf_keeps_page_progress(monkeypatch, tmp_path):
    folder = _isolated_env(monkeypatch, tmp_path)
    path = _write_text_pdf(folder / 'big.pdf', pages=6,
                           text='Chapter 1 Demand ' + 'Marginal cost is the change in total cost. ' * 60)
    task = jobs.enqueue('缺口', path, folder)
    jobs.run_one()
    job = jobs.get(task['id'])
    assert job['status'] == 'succeeded'
    assert job['pages_total'] == 6
    assert job['pages_done'] == 6
