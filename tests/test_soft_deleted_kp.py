"""B2/P1-1: soft-deleted kp stays hidden from every read path and unwritable."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.maintenance import migrations
from app.memory import db, documents, repo, search_index, sources


class SoftDeletedKpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [
            patch.object(db, 'APP_DIR', self.root / 'app'),
            patch.object(db, 'DB_PATH', self.root / 'app' / 'soft_delete.db'),
        ]
        for item in self.patches:
            item.start()
        db.apply_schema()
        conn = db.connect()
        try:
            with conn:
                self.course_id = conn.execute(
                    "INSERT INTO courses(name) VALUES('软删课程')").lastrowid
                self.kp_a = conn.execute(
                    "INSERT INTO kp(course_id,name,gist) VALUES(?,?,'边际成本一句话')",
                    (self.course_id, '边际成本')).lastrowid
                self.kp_b = conn.execute(
                    "INSERT INTO kp(course_id,name,gist) VALUES(?,?,'固定成本一句话')",
                    (self.course_id, '固定成本')).lastrowid
                conn.execute(
                    "INSERT INTO kp_edges(from_kp,to_kp,kind) VALUES(?,?,'related')",
                    (self.kp_a, self.kp_b))
        finally:
            conn.close()
        search_index.rebuild()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def _query(self, sql, params=()):
        conn = db.connect()
        try:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def _scalar(self, sql, params=()):
        conn = db.connect()
        try:
            return conn.execute(sql, params).fetchone()[0]
        finally:
            conn.close()

    def test_search_fts_like_and_rebuild_hide_soft_deleted_kp(self):
        self.assertIn(self.kp_a, {hit['id'] for hit in repo.search('边际成本')})
        self.assertTrue(search_index.search('边际成本'))

        repo.soft_delete_kp(self.kp_a)
        if db.sqlite_capabilities().get('fts5'):
            # 011's frozen trigger re-indexed the row on the soft-delete UPDATE;
            # the read-time filter must hide it anyway.
            self.assertGreater(self._scalar(
                "SELECT COUNT(*) FROM search_fts WHERE kind='kp' AND ref_id=?",
                (str(self.kp_a),)), 0)
        self.assertEqual(repo.search('边际成本'), [])
        self.assertEqual(search_index.search('边际成本'), [])
        self.assertEqual(search_index.search('边际成本', include_dev=True), [])

        # rebuild() also stops indexing the soft-deleted node.
        search_index.rebuild()
        if db.sqlite_capabilities().get('fts5'):
            self.assertEqual(self._scalar(
                "SELECT COUNT(*) FROM search_fts WHERE kind='kp' AND ref_id=?",
                (str(self.kp_a),)), 0)
        self.assertEqual(search_index.search('边际成本'), [])

        # LIKE fallback (FTS-less build) filters the same way.
        with patch.object(search_index._db, 'sqlite_capabilities',
                          return_value={'fts5': False, 'trigram': False}):
            self.assertEqual(search_index.search('边际成本'), [])
            self.assertEqual(repo.search('边际成本'), [])

    def test_restore_makes_the_node_visible_again(self):
        repo.soft_delete_kp(self.kp_a)
        self.assertEqual(repo.search('边际成本'), [])
        conn = db.connect()
        try:
            with conn:
                conn.execute("UPDATE kp SET deleted_at='' WHERE id=?", (self.kp_a,))
        finally:
            conn.close()
        search_index.rebuild()
        self.assertIsNotNone(repo.get_kp(self.kp_a))
        self.assertIn(self.kp_a, {hit['id'] for hit in repo.search('边际成本')})
        self.assertTrue(search_index.search('边际成本'))

    def test_neighbors_skip_soft_deleted_root_and_neighbor(self):
        result = repo.kp_neighbors(self.kp_a)
        self.assertEqual({node['id'] for node in result['nodes']}, {self.kp_b})

        repo.soft_delete_kp(self.kp_b)
        after = repo.kp_neighbors(self.kp_a)
        self.assertEqual(after['nodes'], [])
        self.assertEqual(after['paths'], [])
        self.assertEqual({path['to_kp'] for path in after['paths']}, set())

        repo.soft_delete_kp(self.kp_a)
        with self.assertRaises(ValueError):
            repo.kp_neighbors(self.kp_a)

    def test_write_paths_reject_soft_deleted_kp(self):
        repo.soft_delete_kp(self.kp_a)
        with self.assertRaises(ValueError) as update_error:
            repo.update_kp(self.kp_a, {'gist': '改过的'})
        self.assertIn('已删除', str(update_error.exception))

        mastery = repo.write_mastery(self.kp_a, '熟练')
        self.assertFalse(mastery['ok'])
        self.assertIn('已删除', mastery['error'])
        self.assertEqual(self._scalar('SELECT mastery FROM kp WHERE id=?', (self.kp_a,)), '未读')

        with self.assertRaises(ValueError) as edge_error:
            repo.create_edge(self.kp_a, self.kp_b, 'related')
        self.assertIn('已删除', str(edge_error.exception))
        self.assertEqual(self._scalar(
            "SELECT COUNT(*) FROM kp_edges WHERE from_kp=? AND to_kp=? AND kind='related'",
            (self.kp_a, self.kp_b)), 1)  # only the edge created in setUp

    def test_course_counts_and_mastery_matrix_hide_soft_deleted(self):
        courses = {item['name']: item for item in repo.list_courses()}
        self.assertEqual(courses['软删课程']['kp_count'], 2)
        matrix = repo.mastery_matrix('软删课程')
        self.assertEqual(sum(row['total'] for row in matrix['rows']), 2)

        repo.soft_delete_kp(self.kp_a)
        courses = {item['name']: item for item in repo.list_courses()}
        self.assertEqual(courses['软删课程']['kp_count'], 1)
        matrix = repo.mastery_matrix('软删课程')
        self.assertEqual(sum(row['total'] for row in matrix['rows']), 1)
        self.assertEqual([item['id'] for item in repo.list_kp(course='软删课程')], [self.kp_b])
        self.assertEqual([node['id'] for node in repo.graph_of('软删课程')['nodes']], [self.kp_b])

    def test_documents_find_by_kp_hides_deleted_document(self):
        document = documents.create('studypal', title='固定成本笔记', kp_id=self.kp_b)
        self.assertEqual(documents.find_by_kp(self.kp_b)['id'], document['id'])
        documents.soft_delete(document['id'])
        self.assertIsNone(documents.find_by_kp(self.kp_b))
        # Archived documents are not deleted and remain discoverable by kp.
        restored = documents.restore(document['id'])
        self.assertEqual(restored['status'], 'active')
        self.assertEqual(documents.find_by_kp(self.kp_b)['id'], document['id'])

    def test_capability_record_prefers_probe_over_stale_table(self):
        conn = db.connect()
        try:
            with conn:
                migrations._record_capabilities(
                    conn, {'sqlite_version': 'test', 'fts5': False, 'trigram': False})
            mode = conn.execute(
                "SELECT detail FROM schema_capabilities WHERE name='search_fts'").fetchone()[0]
            self.assertEqual(mode, 'skipped')
            with conn:
                migrations._record_capabilities(
                    conn, {'sqlite_version': 'test', 'fts5': True, 'trigram': False})
            mode = conn.execute(
                "SELECT detail FROM schema_capabilities WHERE name='search_fts'").fetchone()[0]
            self.assertEqual(mode, 'fts5')
        finally:
            conn.close()

    def test_bridges_mark_soft_deleted_endpoint_unresolved(self):
        conn = db.connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO bridges(course_a,kp_a,course_b,kp_b) VALUES(?,?,?,?)",
                    ('软删课程', '边际成本', '软删课程', '固定成本'))
        finally:
            conn.close()
        self.assertEqual(repo.list_bridges()[0]['status'], 'resolved')
        repo.soft_delete_kp(self.kp_b)
        self.assertEqual(repo.list_bridges()[0]['status'], 'unresolved')

    def test_confirm_candidate_revives_soft_deleted_same_name_kp(self):
        conn = db.connect()
        try:
            with conn:
                source_id = conn.execute(
                    "INSERT INTO source_files(course_id,path,sha256,status) VALUES(?,?,?,'indexed')",
                    (self.course_id, 'inbox/revive.md', 'sha-revive')).lastrowid
                candidate_id = conn.execute(
                    "INSERT INTO knowledge_candidates(source_id,schema_version,name,gist,status)"
                    " VALUES(?,?,?,?,'pending')",
                    (source_id, 'cyberpal.knowledge.v1', '边际成本', '候选')).lastrowid
        finally:
            conn.close()
        repo.soft_delete_kp(self.kp_a)
        out = sources.confirm_candidate(candidate_id)
        self.assertEqual(out['status'], 'duplicate')
        self.assertEqual(out['target_kp_id'], self.kp_a)
        row = self._query("SELECT COALESCE(deleted_at,'') AS d FROM kp WHERE id=?", (self.kp_a,))[0]
        self.assertEqual(row['d'], '')


if __name__ == '__main__':
    unittest.main()
