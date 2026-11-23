"""T2.10/O-REL-4: image-only DOCX asks for OCR instead of calling pdftoppm."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from app.memory import db, sources


class DocxOcrTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [
            patch.object(db, 'APP_DIR', self.root / 'app'),
            patch.object(db, 'DB_PATH', self.root / 'app' / 'docx.db'),
        ]
        for item in self.patches:
            item.start()
        db.apply_schema()
        conn = db.connect()
        try:
            with conn:
                conn.execute("INSERT INTO courses(name) VALUES('会计学')")
        finally:
            conn.close()
        self.inbox = self.root / '会计学' / 'inbox'
        self.inbox.mkdir(parents=True)

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def _scalar(self, sql, params=()):
        conn = db.connect()
        try:
            return conn.execute(sql, params).fetchone()[0]
        finally:
            conn.close()

    def _text_docx(self):
        from docx import Document
        document = Document()
        document.add_paragraph('成本核算正文', style='Heading 1')
        document.add_paragraph('边际成本反映新增产量的成本。')
        document.save(str(self.inbox / 'note.docx'))

    def _image_only_docx(self):
        from docx import Document
        image_path = self.root / 'scan.png'
        Image.new('RGB', (120, 40), 'white').save(image_path)
        document = Document()
        document.add_picture(str(image_path))
        document.save(str(self.inbox / 'scan.docx'))

    def _extractor_spy(self, calls):
        def spy(*args, **kwargs):
            calls.append(str(args[0]) if args else str(kwargs))
            return SimpleNamespace(returncode=1, stdout='', stderr='')
        return spy

    def test_text_docx_is_indexed_without_running_pdftoppm(self):
        self._text_docx()
        calls = []
        with patch.object(sources, 'run_extractor', side_effect=self._extractor_spy(calls)):
            result = sources.scan_course('会计学', [self.inbox])
        self.assertEqual(result['indexed'], 1)
        self.assertEqual(self._scalar('SELECT status FROM source_files LIMIT 1'), 'indexed')
        self.assertTrue(self._scalar(
            "SELECT COUNT(*) FROM source_chunks WHERE content LIKE '%边际成本%'"))
        self.assertFalse([call for call in calls if 'pdftoppm' in call])

    def test_image_only_docx_requires_ocr_and_never_runs_pdftoppm(self):
        self._image_only_docx()
        calls = []
        with patch.dict(os.environ, {'MINDOCK_OCR': '', 'MINDOCK_OCR_PYTHON': ''}), \
             patch.object(sources.shutil, 'which',
                          side_effect=lambda name: '/fake/tesseract' if name == 'tesseract' else None), \
             patch.object(sources, '_tesseract_languages', return_value={'chi_sim'}), \
             patch.object(sources, 'run_extractor', side_effect=self._extractor_spy(calls)):
            result = sources.scan_course('会计学', [self.inbox])
        item = result['files'][0]
        self.assertEqual(result['failed'], 1)
        self.assertEqual(item['status'], 'ocr_required')
        self.assertIn('OCR', item['error'])
        self.assertEqual(self._scalar('SELECT COUNT(*) FROM source_chunks'), 0)
        self.assertEqual(self._scalar('SELECT status FROM source_files LIMIT 1'), 'ocr_required')
        self.assertFalse([call for call in calls if 'pdftoppm' in call])

    def test_image_only_docx_without_any_ocr_reports_clear_status(self):
        self._image_only_docx()
        calls = []
        with patch.dict(os.environ, {'MINDOCK_OCR': '', 'MINDOCK_OCR_PYTHON': ''}), \
             patch.object(sources.shutil, 'which', return_value=None), \
             patch.object(sources, 'run_extractor', side_effect=self._extractor_spy(calls)):
            result = sources.scan_course('会计学', [self.inbox])
        item = result['files'][0]
        self.assertEqual(item['status'], 'ocr_required')
        self.assertIn('OCR', item['error'])
        self.assertEqual(calls, [])

    def test_image_only_docx_uses_native_ocr_and_labels_source(self):
        script = self.root / 'fake_ocr.py'
        script.write_text('print("图片里的成本核算文字")', encoding='utf-8')
        self._image_only_docx()
        with patch.dict(os.environ, {'MINDOCK_OCR': str(script),
                                     'MINDOCK_OCR_PYTHON': sys.executable}):
            result = sources.scan_course('会计学', [self.inbox])
        self.assertEqual(result['indexed'], 1)
        self.assertEqual(self._scalar('SELECT status FROM source_files LIMIT 1'), 'indexed')
        conn = db.connect()
        try:
            evidence = [dict(row) for row in conn.execute(
                'SELECT locator_type,content FROM evidence ORDER BY id').fetchall()]
        finally:
            conn.close()
        self.assertTrue(any(item['locator_type'] == 'image_ocr'
                            and '图片里的成本核算文字' in item['content'] for item in evidence))
        self.assertTrue(sources.search('图片里的成本核算文字'))


if __name__ == '__main__':
    unittest.main()
