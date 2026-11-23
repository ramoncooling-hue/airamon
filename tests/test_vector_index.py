import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.memory import db, hybrid_retrieval, search_index, vector_index as vec
from app.maintenance.files import excluded


class TestEmbedding:
    """Deterministic geometry for storage tests, not a semantic model."""
    identity = 'fixture-v1'

    def embed(self, texts):
        return [[1.0, 0.1, 0.0] if 'cost' in text else [0.0, 0.1, 1.0] for text in texts]


@unittest.skipUnless(importlib.util.find_spec('sqlite_vec'), 'optional sqlite-vec')
class VectorIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [patch.object(db, 'APP_DIR', self.root / 'app'),
                        patch.object(db, 'DB_PATH', self.root / 'app/test.db')]
        for p in self.patches:
            p.start()
        db.apply_schema()
        conn = db.connect()
        conn.execute("INSERT INTO courses(name,workspace_id) SELECT 'A',id FROM workspaces WHERE key='studypal'")
        conn.execute("INSERT INTO courses(name,workspace_id) SELECT 'B',id FROM workspaces WHERE key='studypal'")
        conn.execute("INSERT INTO courses(name,workspace_id) SELECT 'Private',id FROM workspaces WHERE key='lifepal'")
        for course in ['A', 'B', 'Private']:
            cid = conn.execute('SELECT id FROM courses WHERE name=?', (course,)).fetchone()[0]
            conn.execute("INSERT INTO kp(course_id,name,gist) VALUES(?,?,?)", (cid, course + ' cost', 'cost definition'))
        cid = conn.execute("SELECT id FROM courses WHERE name='A'").fetchone()[0]
        sid = conn.execute("INSERT INTO source_files(course_id,path,status) VALUES(?,?,'indexed')", (cid, '/fixture/book.txt')).lastrowid
        conn.execute("INSERT INTO source_chunks(source_id,position,content) VALUES(?,0,'cost material')", (sid,))
        self.sid = sid
        conn.commit()
        conn.close()
        self.embedder = TestEmbedding()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_real_extension_query_scoped_to_workspace_and_course(self):
        result = vec.rebuild('studypal', self.embedder)
        self.assertEqual(result['count'], 3)
        hits = vec.search('cost', 'studypal', self.embedder, course='A')['hits']
        self.assertEqual(len(hits), 2)
        self.assertTrue(all(h['course_name'] == 'A' for h in hits))
        self.assertEqual(vec.search('cost', 'studypal', self.embedder, course='Private')['hits'], [])
        self.assertEqual(vec.search('cost', 'lifepal', self.embedder)['status'], 'not_built')
        self.assertTrue(excluded(Path('app/vector-index/test.db')))
        self.assertTrue(excluded(Path('app/extensions/mcp.json')))

    def test_deleted_changed_or_unindexed_content_never_returned(self):
        vec.rebuild('studypal', self.embedder)
        conn = db.connect()
        conn.execute("UPDATE kp SET gist='changed' WHERE name='A cost'")
        conn.execute("DELETE FROM kp WHERE name='B cost'")
        conn.execute("UPDATE source_files SET status='pending' WHERE id=?", (self.sid,))
        conn.commit()
        conn.close()
        result = vec.search('cost', 'studypal', self.embedder)
        self.assertEqual(result['hits'], [])
        self.assertEqual(result['stale_count'], 3)

    def test_soft_deleted_and_stale_rows_excluded_before_and_after_rebuild(self):
        conn = db.connect()
        try:
            with conn:
                old_evidence = conn.execute(
                    "INSERT INTO evidence(source_id,content) VALUES(?,'cost old evidence')",
                    (self.sid,)).lastrowid
                conn.execute("INSERT INTO evidence(source_id,content) VALUES(?,'cost current evidence')",
                             (self.sid,))
                conn.execute("INSERT INTO source_chunks(source_id,position,content) VALUES(?,1,'cost current material')",
                             (self.sid,))
                old_chunk = conn.execute('SELECT id FROM source_chunks WHERE source_id=? AND position=0',
                                         (self.sid,)).fetchone()[0]
                deleted_kp = conn.execute("SELECT id FROM kp WHERE name='A cost'").fetchone()[0]
                source_before = tuple(conn.execute('SELECT status,sha256 FROM source_files WHERE id=?',
                                                   (self.sid,)).fetchone())
        finally:
            conn.close()
        search_index.rebuild()
        self.assertEqual(vec.rebuild('studypal', self.embedder)['count'], 6)
        before = vec.search('cost', 'studypal', self.embedder)['hits']
        self.assertEqual(len(before), 6)
        retired = {('kp', deleted_kp), ('file', old_chunk), ('evidence', old_evidence)}
        expected = {(r['kind'], r['id']): r['content_digest'] for r in before
                    if (r['kind'], r['id']) not in retired}
        expected_a = {(r['kind'], r['id']) for r in before
                      if r['course_name'] == 'A' and (r['kind'], r['id']) not in retired}

        conn = db.connect()
        try:
            with conn:
                conn.execute("UPDATE kp SET deleted_at='2026-10-31' WHERE id=?", (deleted_kp,))
                conn.execute('UPDATE source_chunks SET stale=1 WHERE id=?', (old_chunk,))
                conn.execute('UPDATE evidence SET stale=1 WHERE id=?', (old_evidence,))
            self.assertEqual(tuple(conn.execute('SELECT status,sha256 FROM source_files WHERE id=?',
                                                (self.sid,)).fetchone()), source_before)
        finally:
            conn.close()

        for phase in ('existing', 'rebuilt'):
            if phase == 'rebuilt':
                with self.subTest(phase=phase, path='build'):
                    self.assertEqual(vec.rebuild('studypal', self.embedder)['count'], 3)
            with self.subTest(phase=phase, path='vector'):
                result = vec.search('cost', 'studypal', self.embedder)
                self.assertEqual(result['status'], 'ready')
                self.assertEqual({(r['kind'], r['id']): r['content_digest'] for r in result['hits']}, expected)
                self.assertEqual(result['stale_count'], 3 if phase == 'existing' else 0)
            for course, keys in ((None, set(expected)), ('A', expected_a), ('Private', set())):
                with self.subTest(phase=phase, path='hybrid', course=course):
                    result = hybrid_retrieval.search('cost', 'studypal', self.embedder, course,
                                                    min_similarity=.6)
                    self.assertEqual(result['status'], 'ready')
                    self.assertEqual({(r['kind'], r['id']) for r in result['hits']}, keys)

    def test_soft_delete_and_stale_updates_during_embedding_are_rechecked(self):
        conn = db.connect()
        try:
            with conn:
                conn.execute("INSERT INTO evidence(source_id,content) VALUES(?,'cost evidence')", (self.sid,))
        finally:
            conn.close()
        search_index.rebuild()
        vec.rebuild('studypal', self.embedder)
        before = hybrid_retrieval.search('A cost', 'studypal', self.embedder, 'A', min_similarity=.6)
        self.assertEqual({r['kind'] for r in before['hits']}, {'kp', 'file', 'evidence'})
        original = self.embedder.embed

        def retire_during_query(texts):
            conn = db.connect()
            try:
                with conn:
                    conn.execute("UPDATE kp SET deleted_at='2026-10-31' WHERE name='A cost'")
                    conn.execute('UPDATE source_chunks SET stale=1 WHERE source_id=?', (self.sid,))
                    conn.execute('UPDATE evidence SET stale=1 WHERE source_id=?', (self.sid,))
            finally:
                conn.close()
            return original(texts)

        with patch.object(self.embedder, 'embed', side_effect=retire_during_query):
            result = hybrid_retrieval.search('A cost', 'studypal', self.embedder, 'A', min_similarity=.6)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['hits'], [])

    def test_model_identity_dimension_and_nonfinite_vectors_rejected(self):
        vec.rebuild('studypal', self.embedder)
        self.embedder.identity = 'other-model'
        self.assertEqual(vec.search('cost', 'studypal', self.embedder)['status'], 'rebuild_required')
        self.embedder.identity = 'fixture-v1'
        with patch.object(self.embedder, 'embed', return_value=[[1, 2]]):
            self.assertEqual(vec.search('cost', 'studypal', self.embedder)['status'], 'rebuild_required')
        with patch.object(self.embedder, 'embed', return_value=[[float('nan'), 0, 1]]):
            with self.assertRaises(ValueError):
                vec.rebuild('studypal', self.embedder)

    def test_failed_rebuild_keeps_previous_index(self):
        vec.rebuild('studypal', self.embedder)
        before = vec.index_path('studypal').read_bytes()
        with patch.object(self.embedder, 'embed', side_effect=RuntimeError('offline')):
            with self.assertRaises(RuntimeError):
                vec.rebuild('studypal', self.embedder)
        self.assertEqual(before, vec.index_path('studypal').read_bytes())
        self.assertEqual(len(vec.search('cost', 'studypal', self.embedder)['hits']), 3)

    def test_changes_during_embedding_are_rechecked_before_results(self):
        vec.rebuild('studypal', self.embedder)
        original = self.embedder.embed

        def concurrent_change(texts):
            conn = db.connect()
            conn.execute("DELETE FROM kp WHERE name='A cost'")
            conn.execute("UPDATE courses SET workspace_id=(SELECT id FROM workspaces WHERE key='lifepal') WHERE name='B'")
            conn.commit()
            conn.close()
            return original(texts)

        with patch.object(self.embedder, 'embed', side_effect=concurrent_change):
            hits = vec.search('cost', 'studypal', self.embedder)['hits']
        self.assertEqual([h['kind'] for h in hits], ['file'])

    def test_unknown_workspace_and_empty_index(self):
        with self.assertRaises(ValueError):
            vec.rebuild('unknown', self.embedder)
        result = vec.rebuild('workpal', self.embedder)
        self.assertEqual(result['count'], 0)
        self.assertEqual(vec.search('cost', 'workpal', self.embedder)['hits'], [])

    def test_hybrid_real_index_preserves_identity_and_live_scope(self):
        conn = db.connect()
        conn.execute("INSERT INTO evidence(source_id,content) VALUES(?,'cost evidence')", (self.sid,))
        conn.commit()
        conn.close()
        search_index.rebuild()
        vec.rebuild('studypal', self.embedder)
        result = hybrid_retrieval.search('cost', 'studypal', self.embedder, 'A', min_similarity=.6)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual({r['kind'] for r in result['hits']}, {'kp', 'file', 'evidence'})
        self.assertEqual(len(result['hits']), 3)
        self.assertTrue(all(r['course_name'] == 'A' for r in result['hits']))
        self.assertEqual(hybrid_retrieval.search('cost', 'studypal', self.embedder,
                         'Private', min_similarity=.6)['hits'], [])

        # Mutate while the semantic query runs, after keyword candidates were read.
        original = self.embedder.embed
        def change_during_query(texts):
            conn = db.connect()
            conn.execute("UPDATE kp SET gist='new content' WHERE name='A cost'")
            conn.execute('DELETE FROM evidence WHERE source_id=?', (self.sid,))
            conn.execute("UPDATE source_files SET status='pending' WHERE id=?", (self.sid,))
            conn.commit()
            conn.close()
            return original(texts)
        with patch.object(self.embedder, 'embed', side_effect=change_during_query):
            result = hybrid_retrieval.search('A cost', 'studypal', self.embedder, 'A', min_similarity=.6)
        self.assertEqual(result['hits'], [])

    def test_nonmaterial_matches_cannot_exhaust_keyword_candidate_limit(self):
        conn = db.connect()
        conn.execute('DELETE FROM search_fts')
        for i in range(60):
            conn.execute("INSERT INTO search_fts(kind,ref_id,course_name,title,body) VALUES('notice',?,'A','cost','cost')", (str(i),))
        kid = conn.execute("SELECT id FROM kp WHERE name='A cost'").fetchone()[0]
        conn.execute("INSERT INTO search_fts(kind,ref_id,course_name,title,body) VALUES('kp',?,'A','A cost','cost')", (str(kid),))
        conn.commit()
        conn.close()
        # 'ost' misses FTS tokens and exercises the LIKE fallback too.
        for query in ('cost', 'ost'):
            rows = hybrid_retrieval._keyword(query, 'studypal', 'A', 50)
            self.assertEqual([(r['kind'], r['id']) for r in rows], [('kp', kid)])


class LocalEmbeddingTests(unittest.TestCase):
    def test_only_explicit_loopback_endpoint_allowed(self):
        for endpoint in ['https://example.com/v1/embeddings', 'http://localhost:11434/v1/embeddings',
                         f"http://{'user'}:{'pass'}@127.0.0.1:1234/v1/embeddings",
                         'http://127.0.0.1/v1/embeddings?token=x']:
            with self.assertRaises(ValueError):
                vec.LocalEmbedding(endpoint, 'test')
        self.assertTrue(vec.LocalEmbedding('http://127.0.0.1:11434/v1/embeddings', 'test').identity)

    def test_response_order_and_count_validated(self):
        embedding = vec.LocalEmbedding('http://127.0.0.1:11434/v1/embeddings', 'test')
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, n):
                return json.dumps({'data': [
                    {'index': 1, 'embedding': [0, 1]}, {'index': 0, 'embedding': [1, 0]}
                ]}).encode()
        with patch.object(embedding.opener, 'open', return_value=Response()):
            self.assertEqual(embedding.embed(['a', 'b']), [[1, 0], [0, 1]])
            with self.assertRaises(ValueError):
                embedding.embed(['a'])
