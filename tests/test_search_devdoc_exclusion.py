"""T2.9/O-DATA-7: development_doc material stays out of retrieval by default."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.api import routes
from app.memory import db, repo, search_index, sources

DEV_QUERY = '开发文档专属术语'
DEV_KP_QUERY = '图谱开发节点'
NORMAL_QUERY = '成本核算正文'


class DevelopmentDocExclusionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [
            patch.object(db, 'APP_DIR', self.root / 'app'),
            patch.object(db, 'DB_PATH', self.root / 'app' / 'search.db'),
        ]
        for item in self.patches:
            item.start()
        db.apply_schema()
        conn = db.connect()
        try:
            with conn:
                conn.execute("INSERT INTO courses(name) VALUES('会计学')")
                course_id = conn.execute(
                    "SELECT id FROM courses WHERE name='会计学'").fetchone()[0]
                conn.execute(
                    """INSERT INTO source_files(course_id,path,sha256,status,source_kind,source_role)
                       VALUES(?,?,?,'indexed','development_doc','platform_document')""",
                    (course_id, '/tmp/dev-wiki.md', 'devhash'))
                self.dev_source_id = conn.execute(
                    'SELECT id FROM source_files WHERE path=?', ('/tmp/dev-wiki.md',)).fetchone()[0]
                conn.execute(
                    'INSERT INTO source_chunks(source_id,position,content) VALUES(?,?,?)',
                    (self.dev_source_id, 0, DEV_QUERY + ' 开发内部链接'))
                self.dev_chunk_id = conn.execute(
                    'SELECT id FROM source_chunks WHERE source_id=?',
                    (self.dev_source_id,)).fetchone()[0]
                conn.execute(
                    """INSERT INTO evidence(source_id,locator_type,locator_start,locator_end,
                                            content,content_hash)
                       VALUES(?,?,?,?,?,?)""",
                    (self.dev_source_id, 'line', 1, 1, DEV_QUERY + ' 开发内部链接', 'devhash-1'))
                self.dev_evidence_id = conn.execute(
                    'SELECT id FROM evidence WHERE source_id=?',
                    (self.dev_source_id,)).fetchone()[0]
                conn.execute(
                    'INSERT INTO kp(course_id,name,gist,source) VALUES(?,?,?,?)',
                    (course_id, DEV_KP_QUERY, '开发专属说明', '/tmp/dev-wiki.md'))
                self.dev_kp_id = conn.execute(
                    'SELECT id FROM kp WHERE name=?', (DEV_KP_QUERY,)).fetchone()[0]
                conn.execute(
                    """INSERT INTO source_files(course_id,path,sha256,status,source_kind)
                       VALUES(?,?,?,'indexed','textbook')""",
                    (course_id, '/tmp/textbook.md', 'normhash'))
                normal_source_id = conn.execute(
                    'SELECT id FROM source_files WHERE path=?', ('/tmp/textbook.md',)).fetchone()[0]
                conn.execute(
                    'INSERT INTO source_chunks(source_id,position,content) VALUES(?,?,?)',
                    (normal_source_id, 0, NORMAL_QUERY + ' 学习区材料'))
        finally:
            conn.close()

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

    def _fts5(self):
        return bool(db.sqlite_capabilities().get('fts5'))

    def test_development_doc_hidden_by_default_but_visible_with_include_dev(self):
        if self._fts5():
            # 011's trigger indexed the dev rows on insert; read filters hide them.
            self.assertGreater(self._scalar(
                "SELECT COUNT(*) FROM search_fts WHERE kind='source_chunk' AND ref_id=?",
                (str(self.dev_chunk_id),)), 0)
            self.assertGreater(self._scalar(
                "SELECT COUNT(*) FROM search_fts WHERE kind='evidence' AND ref_id=?",
                (str(self.dev_evidence_id),)), 0)
        self.assertEqual(search_index.search(DEV_QUERY), [])
        self.assertTrue(search_index.search(DEV_QUERY, include_dev=True))
        self.assertEqual(sources.search(DEV_QUERY), [])
        self.assertTrue(sources.search(DEV_QUERY, include_dev=True))
        self.assertEqual(repo.search(DEV_QUERY), [])
        self.assertTrue(repo.search(DEV_QUERY, include_dev=True))
        self.assertEqual(routes.search(DEV_QUERY)['hits'], [])
        self.assertTrue(routes.search(DEV_QUERY, include_dev=1)['hits'])

    def test_rebuild_stops_indexing_development_docs(self):
        if not self._fts5():
            self.assertEqual(search_index.rebuild(), 0)
            self.assertEqual(search_index.search(DEV_QUERY), [])
            return
        search_index.rebuild()
        self.assertEqual(self._scalar(
            "SELECT COUNT(*) FROM search_fts WHERE kind='source_chunk' AND ref_id=?",
            (str(self.dev_chunk_id),)), 0)
        self.assertEqual(self._scalar(
            "SELECT COUNT(*) FROM search_fts WHERE kind='evidence' AND ref_id=?",
            (str(self.dev_evidence_id),)), 0)
        self.assertEqual(self._scalar(
            "SELECT COUNT(*) FROM search_fts WHERE kind='kp' AND ref_id=?",
            (str(self.dev_kp_id),)), 0)
        self.assertEqual(search_index.search(DEV_QUERY), [])
        self.assertEqual(search_index.search(DEV_QUERY, include_dev=True), [])
        self.assertTrue(search_index.search(NORMAL_QUERY))

    def test_development_doc_kp_stays_out_of_field_fallback(self):
        self.assertEqual(search_index.search(DEV_KP_QUERY), [])
        self.assertGreater(len(search_index.search(DEV_KP_QUERY, include_dev=True)), 0)
        # Field-search fallback (used when FTS is unavailable) filters the same way.
        self.assertEqual(repo._search_one(DEV_KP_QUERY, 10, None), [])
        self.assertTrue(repo._search_one(DEV_KP_QUERY, 10, None, include_dev=True))
        self.assertEqual(repo.search(DEV_KP_QUERY), [])
        self.assertTrue(repo.search(DEV_KP_QUERY, include_dev=True))

    def test_normal_material_is_unaffected(self):
        self.assertTrue(search_index.search(NORMAL_QUERY))
        self.assertTrue(sources.search(NORMAL_QUERY))
        self.assertTrue(repo.search(NORMAL_QUERY))
        found = routes.search(NORMAL_QUERY)['hits']
        self.assertTrue(found)
        self.assertNotIn('/tmp/dev-wiki.md', {hit.get('source', '') for hit in found})


if __name__ == '__main__':
    unittest.main()
