"""T1.6/O-DATA-1: a rescan retires history instead of deleting reviewed data."""

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.memory import db, sources, search_index


class RescanPreservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [
            patch.object(db, 'APP_DIR', self.root / 'app'),
            patch.object(db, 'DB_PATH', self.root / 'app' / 'rescan.db'),
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
        self.doc = self.inbox / 'lesson.md'
        self.doc.write_text(
            '# 成本核算\n边际成本反映新增产量的成本。\n# 固定成本\n固定成本不随产量变化。',
            encoding='utf-8')
        self.scan = sources.scan_course('会计学', [self.inbox])

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

    def _candidates(self):
        return {item['label']: item for item in sources.candidates('会计学')}

    def _make_legacy(self, *, keep_snapshot=False):
        conn = db.connect()
        try:
            with conn:
                for table in ('source_chunks', 'evidence', 'knowledge_candidates'):
                    conn.execute(f'UPDATE {table} SET snapshot_id=NULL')
                if not keep_snapshot:
                    conn.execute('DELETE FROM source_snapshots')
        finally:
            conn.close()

    def test_legacy_rescan_verifies_evidence_and_preserves_manual_content(self):
        confirmed = sources.confirm_candidate(self._candidates()['成本核算']['id'])
        sources.review_candidate(self._candidates()['固定成本']['id'], 'rejected')
        conn = db.connect()
        try:
            with conn:
                conn.execute("UPDATE kp SET mastery='会用',gist='人工修订内容' WHERE id=?",
                             (confirmed['target_kp_id'],))
                conn.execute('''INSERT INTO evidence
                    (source_id,locator_type,locator_start,locator_end,content,content_hash)
                    SELECT id,'line',99,99,?,? FROM source_files''',
                    ('旧抽取残留，原文中不存在', hashlib.sha256('旧抽取残留'.encode()).hexdigest()))
        finally:
            conn.close()
        self._make_legacy()
        self.assertEqual(self._scalar('SELECT COUNT(*) FROM source_snapshots'), 0)
        note = Path(self.scan['files'][0]['artifacts']['note'])
        dictionary = Path(self.scan['files'][0]['artifacts']['dictionary'])
        note.write_bytes(b'# Hand edited note\r\nKeep this exactly.\r\n')
        dictionary.write_bytes(b'{"manual": "keep exactly"}\n')
        artifact_bytes = {path: path.read_bytes() for path in (note, dictionary)}
        protected = ('kp', 'kp_edges', 'kp_evidence', 'revisions', 'candidate_reviews',
                     'knowledge_candidates', 'graph_candidates', 'knowledge_notes')
        before = {table: self._query(f'SELECT * FROM {table} ORDER BY rowid') for table in protected}
        expected = {item['text'] for item in sources.extract_structured(self.doc)[2]}

        with patch.object(sources, 'extract_structured', wraps=sources.extract_structured) as extract, \
                patch.object(sources.wiki, 'write_artifacts') as write_artifacts:
            result = sources.scan_course('会计学', [self.inbox])
            self.assertEqual(result['indexed'], 1)
            self.assertEqual(result['unchanged'], 0)
            extract.assert_called_once_with(self.doc.resolve())
            write_artifacts.assert_not_called()
        current = self._query('''SELECT e.content,ss.sha256 FROM evidence e
            JOIN source_snapshots ss ON ss.id=e.snapshot_id AND ss.source_id=e.source_id
            JOIN source_files sf ON sf.id=e.source_id
            WHERE e.stale=0 AND ss.sha256=sf.sha256''')
        self.assertEqual({row['content'] for row in current}, expected)
        self.assertEqual({row['sha256'] for row in current}, {sources.file_digest(self.doc)})
        self.assertEqual(self._scalar('''SELECT COUNT(*) FROM evidence
            WHERE locator_start=99 AND snapshot_id IS NULL AND stale=1'''), 1)
        self.assertEqual(self._scalar('SELECT COUNT(*) FROM source_snapshots'), 1)
        self.assertEqual(self._query('PRAGMA foreign_key_check'), [])
        for table in protected:
            self.assertEqual(self._query(f'SELECT * FROM {table} ORDER BY rowid'), before[table], table)
        for path, content in artifact_bytes.items():
            self.assertEqual(path.read_bytes(), content)

        tables = (*protected, 'source_chunks', 'evidence', 'source_snapshots',
                  'source_versions', 'source_version_artifacts')
        upgraded = {table: self._query(f'SELECT * FROM {table} ORDER BY rowid') for table in tables}
        with patch.object(sources, 'extract_structured') as extract:
            self.assertEqual(sources.scan_course('会计学', [self.inbox])['unchanged'], 1)
            extract.assert_not_called()
        for table in tables:
            self.assertEqual(self._query(f'SELECT * FROM {table} ORDER BY rowid'), upgraded[table], table)
        for path, content in artifact_bytes.items():
            self.assertEqual(path.read_bytes(), content)

    def test_current_snapshot_without_versioned_evidence_is_rebuilt(self):
        self._make_legacy(keep_snapshot=True)
        result = sources.scan_course('会计学', [self.inbox])
        self.assertEqual(result['indexed'], 1)
        self.assertGreater(self._scalar(
            'SELECT COUNT(*) FROM evidence WHERE stale=0 AND snapshot_id IS NOT NULL'), 0)
        self.assertEqual(self._scalar('SELECT COUNT(*) FROM source_snapshots'), 1)

    def test_snapshot_for_another_hash_does_not_skip_reextraction(self):
        conn = db.connect()
        try:
            with conn:
                conn.execute('UPDATE source_snapshots SET sha256=?', ('0' * 64,))
        finally:
            conn.close()
        result = sources.scan_course('会计学', [self.inbox])
        self.assertEqual(result['indexed'], 1)
        self.assertGreater(self._scalar('''SELECT COUNT(*) FROM evidence e
            JOIN source_snapshots ss ON ss.id=e.snapshot_id
            JOIN source_files sf ON sf.id=e.source_id
            WHERE e.stale=0 AND ss.sha256=sf.sha256'''), 0)

    def test_partial_legacy_evidence_is_rebuilt(self):
        conn = db.connect()
        try:
            with conn:
                conn.execute('UPDATE evidence SET snapshot_id=NULL WHERE id=(SELECT MIN(id) FROM evidence)')
        finally:
            conn.close()
        result = sources.scan_course('会计学', [self.inbox])
        self.assertEqual(result['indexed'], 1)
        self.assertEqual(self._scalar(
            'SELECT COUNT(*) FROM evidence WHERE stale=0 AND snapshot_id IS NULL'), 0)

    def test_only_stale_evidence_does_not_skip_reextraction(self):
        conn = db.connect()
        try:
            with conn:
                conn.execute('UPDATE evidence SET stale=1')
        finally:
            conn.close()
        result = sources.scan_course('会计学', [self.inbox])
        self.assertEqual(result['indexed'], 1)
        self.assertGreater(self._scalar('SELECT COUNT(*) FROM evidence WHERE stale=0'), 0)

    def test_current_evidence_with_legacy_chunks_is_rebuilt(self):
        conn = db.connect()
        try:
            with conn:
                conn.execute('UPDATE source_chunks SET snapshot_id=NULL')
        finally:
            conn.close()
        result = sources.scan_course('会计学', [self.inbox])
        self.assertEqual(result['indexed'], 1)
        self.assertEqual(self._scalar(
            'SELECT COUNT(*) FROM source_chunks WHERE stale=0 AND snapshot_id IS NULL'), 0)

    def test_failed_legacy_retry_preserves_existing_artifacts(self):
        self._make_legacy()
        artifacts = [Path(value) for value in self.scan['files'][0]['artifacts'].values()]
        for path in artifacts:
            path.write_bytes(b'Hand edited; retain even after a failed repair.\r\n')
        before = {path: path.read_bytes() for path in artifacts}
        notes = self._query('SELECT * FROM knowledge_notes')
        with patch.object(sources, 'extract_structured', side_effect=ValueError('暂时无法提取')):
            self.assertEqual(sources.scan_course('会计学', [self.inbox])['failed'], 1)
        with patch.object(sources.wiki, 'write_artifacts') as write_artifacts:
            self.assertEqual(sources.scan_course('会计学', [self.inbox])['indexed'], 1)
            write_artifacts.assert_not_called()
        self.assertEqual(self._query('SELECT * FROM knowledge_notes'), notes)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_confirmed_candidate_reviews_and_links_survive_rescan(self):
        target = self._candidates()['成本核算']
        confirmed = sources.confirm_candidate(target['id'])
        kp_id = confirmed['target_kp_id']
        reviews_before = self._scalar(
            'SELECT COUNT(*) FROM candidate_reviews WHERE candidate_id=?', (target['id'],))
        links_before = self._scalar('SELECT COUNT(*) FROM kp_evidence WHERE kp_id=?', (kp_id,))
        self.assertGreaterEqual(links_before, 1)
        positional_before = self._scalar(
            'SELECT MAX(position) FROM source_chunks WHERE stale=0')

        self.doc.write_text(
            '# 成本核算\n边际成本提高。\n# 固定成本\n固定成本不随产量变化。\n# 机会成本\n机会成本是被放弃的收益。',
            encoding='utf-8')
        result = sources.scan_course('会计学', [self.inbox])
        self.assertEqual(result['indexed'], 1)

        candidates = self._candidates()
        self.assertEqual(candidates['成本核算']['status'], 'confirmed')
        self.assertEqual(candidates['机会成本']['status'], 'pending')
        self.assertEqual(self._scalar(
            "SELECT COUNT(*) FROM knowledge_candidates WHERE name='成本核算'"), 1)
        self.assertEqual(self._scalar(
            'SELECT COUNT(*) FROM candidate_reviews WHERE candidate_id=?', (target['id'],)),
            reviews_before)
        self.assertGreaterEqual(self._scalar(
            'SELECT COUNT(*) FROM kp_evidence WHERE kp_id=?', (kp_id,)), links_before)
        self.assertGreaterEqual(self._scalar(
            'SELECT COUNT(*) FROM source_chunks WHERE stale=1'), 1)
        self.assertGreaterEqual(self._scalar(
            'SELECT COUNT(*) FROM evidence WHERE stale=1'), 1)
        # New generations continue after the retained rows instead of
        # colliding with UNIQUE(source_id,position).
        self.assertEqual(self._scalar(
            'SELECT COUNT(*) FROM (SELECT position FROM source_chunks GROUP BY position HAVING COUNT(*)>1)'), 0)
        self.assertGreater(
            self._scalar('SELECT MAX(position) FROM source_chunks WHERE stale=0'), positional_before)

    def test_read_paths_only_serve_the_current_snapshot(self):
        self.doc.write_text('# 成本核算\n边际成本提高。', encoding='utf-8')
        sources.scan_course('会计学', [self.inbox])
        hits = sources.search('边际成本', '会计学')
        self.assertEqual(len(hits), 1)
        self.assertIn('提高', hits[0]['gist'])
        self.assertEqual(self._scalar(
            "SELECT COUNT(*) FROM source_chunks WHERE stale=1 AND content LIKE '%反映新增产量%'"), 1)
        status = sources.status('会计学')
        self.assertEqual(status[0]['chunks'], self._scalar(
            'SELECT COUNT(*) FROM source_chunks WHERE stale=0'))
        # The FTS rebuild must not index the retired rows (ref_ids are only
        # unique per kind, so compare qualified ids).
        search_index.rebuild()
        stale_chunk_ids = {str(row['id']) for row in self._query(
            'SELECT id FROM source_chunks WHERE stale=1')}
        stale_evidence_ids = {str(row['id']) for row in self._query(
            'SELECT id FROM evidence WHERE stale=1')}
        indexed = {(item['kind'], item['ref_id']) for item in search_index.search('边际成本', None, 50)}
        self.assertFalse({('source_chunk', ref) for ref in stale_chunk_ids} & indexed)
        self.assertFalse({('evidence', ref) for ref in stale_evidence_ids} & indexed)

    def test_confirmation_after_rescan_prefers_live_evidence(self):
        self.doc.write_text(
            '# 成本核算\n边际成本提高。\n# 机会成本\n机会成本是被放弃的收益。',
            encoding='utf-8')
        sources.scan_course('会计学', [self.inbox])
        candidate = self._candidates()['机会成本']
        confirmed = sources.confirm_candidate(candidate['id'])
        rows = self._query(
            '''SELECT e.stale FROM kp_evidence ke JOIN evidence e ON e.id=ke.evidence_id
               WHERE ke.kp_id=?''', (confirmed['target_kp_id'],))
        self.assertTrue(rows)
        self.assertTrue(all(row['stale'] == 0 for row in rows))

    def test_rejected_candidate_history_is_not_resurrected(self):
        rejected = self._candidates()['固定成本']
        sources.review_candidate(rejected['id'], 'rejected')
        reviews_before = self._scalar(
            'SELECT COUNT(*) FROM candidate_reviews WHERE candidate_id=?', (rejected['id'],))
        self.doc.write_text(
            '# 成本核算\n边际成本提高。\n# 固定成本\n固定成本不随产量变化。',
            encoding='utf-8')
        sources.scan_course('会计学', [self.inbox])
        self.assertEqual(self._candidates()['固定成本']['status'], 'rejected')
        self.assertEqual(self._scalar(
            'SELECT COUNT(*) FROM knowledge_candidates WHERE name=?', ('固定成本',)), 1)
        self.assertEqual(self._scalar(
            'SELECT COUNT(*) FROM candidate_reviews WHERE candidate_id=?', (rejected['id'],)),
            reviews_before)

    def test_unchanged_rescan_does_not_add_extraction_rows(self):
        chunks_before = self._scalar('SELECT COUNT(*) FROM source_chunks')
        evidence_before = self._scalar('SELECT COUNT(*) FROM evidence')
        snapshots_before = self._scalar('SELECT COUNT(*) FROM source_snapshots')
        result = sources.scan_course('会计学', [self.inbox])
        self.assertEqual(result['unchanged'], 1)
        self.assertEqual(self._scalar('SELECT COUNT(*) FROM source_chunks'), chunks_before)
        self.assertEqual(self._scalar('SELECT COUNT(*) FROM evidence'), evidence_before)
        self.assertEqual(self._scalar('SELECT COUNT(*) FROM source_snapshots'), snapshots_before)
        self.assertTrue(self._scalar('SELECT COUNT(*) FROM source_snapshots') >= 1)


if __name__ == '__main__':
    unittest.main()
