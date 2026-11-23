import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.memory import db, sources


class SourceVersionTests(unittest.TestCase):
    def test_changed_file_keeps_previous_sha_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'):
                db.apply_schema()
                conn = db.connect()
                conn.execute("INSERT INTO courses(name) VALUES('测试课程')")
                conn.commit()
                conn.close()
                inbox = root / '测试课程' / 'inbox'
                inbox.mkdir(parents=True)
                doc = inbox / 'lesson.md'
                doc.write_text('# 第一版\n内容一', encoding='utf-8')
                sources.scan_course('测试课程', [inbox])
                doc.write_text('# 第二版\n内容二', encoding='utf-8')
                sources.scan_course('测试课程', [inbox])
                conn = db.connect()
                try:
                    versions = conn.execute('SELECT COUNT(*) FROM source_versions').fetchone()[0]
                finally:
                    conn.close()
                self.assertEqual(versions, 2)

                conn = db.connect()
                try:
                    snapshot = conn.execute('SELECT sha256,raw_path,text_path FROM source_version_artifacts ORDER BY id LIMIT 1').fetchone()
                finally:
                    conn.close()
                self.assertTrue(Path(snapshot['raw_path']).exists())
                self.assertTrue(Path(snapshot['text_path']).exists())
                diff = sources.diff_versions('测试课程', str(doc.resolve()), snapshot['sha256'], 'latest')
                self.assertIn('-内容一', diff['diff'])
                self.assertIn('+内容二', diff['diff'])

    def test_unchanged_inbox_file_backfills_missing_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'):
                db.apply_schema()
                conn = db.connect()
                conn.execute("INSERT INTO courses(name) VALUES('测试课程')")
                conn.commit()
                conn.close()
                inbox = root / '测试课程' / 'inbox'
                inbox.mkdir(parents=True)
                doc = inbox / 'lesson.md'
                doc.write_text('# 课程\n正文', encoding='utf-8')
                sources.scan_course('测试课程', [inbox])
                conn = db.connect()
                conn.execute('DELETE FROM source_version_artifacts')
                conn.commit()
                conn.close()
                self.assertEqual(sources.scan_course('测试课程', [inbox])['unchanged'], 1)
                conn = db.connect()
                try:
                    count = conn.execute('SELECT COUNT(*) FROM source_version_artifacts').fetchone()[0]
                finally:
                    conn.close()
                self.assertEqual(count, 1)

    def test_missing_inbox_file_becomes_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'):
                db.apply_schema()
                conn = db.connect()
                conn.execute("INSERT INTO courses(name) VALUES('测试课程')")
                conn.commit()
                conn.close()
                inbox = root / '测试课程' / 'inbox'
                inbox.mkdir(parents=True)
                doc = inbox / 'lesson.md'
                doc.write_text('# 课程', encoding='utf-8')
                sources.scan_course('测试课程', [inbox])
                doc.unlink()
                result = sources.scan_course('测试课程', [inbox])
                self.assertEqual(result['stale'], 1)
                self.assertEqual(sources.status('测试课程')[0]['status'], 'stale')

    def test_stale_file_can_be_restored_from_version_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'):
                db.apply_schema()
                conn = db.connect()
                conn.execute("INSERT INTO courses(name) VALUES('测试课程')")
                conn.commit()
                conn.close()
                inbox = root / '测试课程' / 'inbox'
                inbox.mkdir(parents=True)
                doc = inbox / 'lesson.md'
                doc.write_text('# 可恢复\n原始内容', encoding='utf-8')
                sources.scan_course('测试课程', [inbox])
                conn = db.connect()
                sha = conn.execute('SELECT sha256 FROM source_versions ORDER BY id LIMIT 1').fetchone()['sha256']
                conn.close()
                doc.unlink()
                sources.scan_course('测试课程', [inbox])
                restored = sources.restore_version('测试课程', str(doc.resolve()), sha)
                self.assertEqual(restored['status'], 'indexed')
                self.assertIn('原始内容', doc.read_text(encoding='utf-8'))


class LegacySourceVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, value in [('APP_DIR', self.root / 'app'),
                            ('DB_PATH', self.root / 'app' / 'legacy.db')]:
            item = patch.object(db, name, value)
            item.start()
            self.addCleanup(item.stop)
        db.apply_schema()
        self.inbox = self.root / '测试课程' / 'inbox'
        self.inbox.mkdir(parents=True)
        self.doc = self.inbox / 'legacy.md'
        self.doc.write_text('# 原文件\n需要重新核验的正文', encoding='utf-8')
        self.digest = sources.file_digest(self.doc)
        conn = db.connect()
        try:
            with conn:
                cid = conn.execute("INSERT INTO courses(name) VALUES('测试课程')").lastrowid
                sid = conn.execute('''INSERT INTO source_files(course_id,path,sha256,status)
                    VALUES(?,?,?,'indexed')''', (cid, str(self.doc.resolve()), self.digest)).lastrowid
                conn.execute('INSERT INTO source_chunks(source_id,position,content) VALUES(?,0,?)',
                             (sid, '无法核验的旧内容'))
                conn.execute('''INSERT INTO evidence
                    (source_id,locator_type,locator_start,locator_end,content,content_hash)
                    VALUES(?,'line',1,1,?,'legacy')''', (sid, '无法核验的旧内容'))
        finally:
            conn.close()

    def _assert_failed_without_new_version(self, result):
        self.assertEqual(result['failed'], 1)
        self.assertEqual(result['indexed'], 0)
        self.assertEqual(result['unchanged'], 0)
        conn = db.connect()
        try:
            for table in ('source_snapshots', 'source_versions', 'source_version_artifacts', 'knowledge_notes'):
                self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0, table)
            source = conn.execute('SELECT sha256,status FROM source_files').fetchone()
            self.assertEqual(source['sha256'], self.digest)
            self.assertEqual(source['status'], 'error')
            self.assertEqual(tuple(conn.execute(
                'SELECT content,snapshot_id,stale FROM evidence').fetchone()),
                ('无法核验的旧内容', None, 0))
        finally:
            conn.close()
        self.assertFalse((self.inbox.parent / 'source').exists())

    def test_legacy_extraction_error_fails_closed(self):
        with patch.object(sources, 'extract_structured', side_effect=ValueError('无法提取原文件')):
            result = sources.scan_course('测试课程', [self.inbox])
        self._assert_failed_without_new_version(result)

    def test_legacy_empty_extraction_fails_closed(self):
        with patch.object(sources, 'extract_structured', return_value=('', [], [])):
            result = sources.scan_course('测试课程', [self.inbox])
        self._assert_failed_without_new_version(result)

    def test_legacy_extraction_without_evidence_fails_closed(self):
        with patch.object(sources, 'extract_structured', return_value=('只有文字', [], [])):
            result = sources.scan_course('测试课程', [self.inbox])
        self._assert_failed_without_new_version(result)

    def test_legacy_file_changed_during_extraction_fails_closed(self):
        extract = sources.extract_structured

        def changing_extract(path):
            extracted = extract(path)
            path.write_text('# 已变化\n不能绑定旧hash', encoding='utf-8')
            return extracted

        with patch.object(sources, 'extract_structured', side_effect=changing_extract):
            result = sources.scan_course('测试课程', [self.inbox])
        self._assert_failed_without_new_version(result)

    def test_legacy_expected_digest_rejects_changed_original(self):
        self.doc.write_text('# 已变化\n必须重新核验', encoding='utf-8')
        result = sources.scan_course('测试课程', [self.inbox], expected_digest=self.digest)
        self._assert_failed_without_new_version(result)

    def _assert_recovers_without_prior_derivatives(self, status):
        conn = db.connect()
        try:
            with conn:
                conn.execute('UPDATE source_files SET status=?', (status,))
        finally:
            conn.close()
        result = sources.scan_course('测试课程', [self.inbox], force_ocr=True)
        self.assertEqual(result['indexed'], 1)
        conn = db.connect()
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM knowledge_candidates').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM knowledge_notes').fetchone()[0], 1)
        finally:
            conn.close()
        for key in ('note', 'dictionary'):
            self.assertTrue(Path(result['files'][0]['artifacts'][key]).is_file())

    def test_same_hash_error_without_derivatives_recovers_normally(self):
        self._assert_recovers_without_prior_derivatives('error')

    def test_same_hash_ocr_required_without_derivatives_recovers_normally(self):
        self._assert_recovers_without_prior_derivatives('ocr_required')


if __name__ == '__main__':
    unittest.main()
