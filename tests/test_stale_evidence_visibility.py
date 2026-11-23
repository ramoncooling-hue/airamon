"""B1.1/P2-3+P2-4: stale evidence must not leak into user-visible reads, and
pending candidates whose heading vanished on a rescan must be superseded."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.memory import db, relation_suggestions, repo, sources


class StaleEvidenceVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [
            patch.object(db, 'APP_DIR', self.root / 'app'),
            patch.object(db, 'DB_PATH', self.root / 'app' / 'stale.db'),
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
            '# 成本核算\n边际成本反映新增产量的成本。\n'
            '# 固定成本\n固定成本不随产量变化。\n'
            '# 机会成本\n机会成本是被放弃的收益。',
            encoding='utf-8')
        sources.scan_course('会计学', [self.inbox])
        self.kp_id = sources.confirm_candidate(self._candidates()['成本核算']['id'])['target_kp_id']

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

    def _insert_concept_candidate(self, name):
        """A model-assisted concept candidate: it never carries snapshot_id."""
        source_id = self._scalar('SELECT id FROM source_files LIMIT 1')
        conn = db.connect()
        try:
            with conn:
                cursor = conn.execute(
                    '''INSERT INTO knowledge_candidates
                       (source_id,schema_version,name,kp_type,chapter,gist,source,confidence,status)
                       VALUES(?,?,?,'concept','','概念候选','',0.55,'pending')''',
                    (source_id, 'cyberpal.knowledge.v1', name))
            return cursor.lastrowid
        finally:
            conn.close()

    def _rescan(self, text):
        self.doc.write_text(text, encoding='utf-8')
        return sources.scan_course('会计学', [self.inbox])

    def _stale_evidence_ids(self):
        return {row['id'] for row in self._query('SELECT id FROM evidence WHERE stale=1')}

    def test_kp_detail_and_graph_source_refs_hide_stale_evidence(self):
        self._rescan(
            '# 成本核算\n边际成本提高。\n'
            '# 固定成本\n固定成本提高。\n'
            '# 机会成本\n机会成本提高。')
        stale_ids = self._stale_evidence_ids()
        self.assertTrue(stale_ids)

        detail = repo.get_kp(self.kp_id)
        self.assertFalse({item['id'] for item in detail['evidence']} & stale_ids)
        self.assertFalse({item['id'] for item in repo.kp_evidence(self.kp_id)} & stale_ids)

        graph = repo.graph_of('会计学')
        refs = [ref for node in graph['nodes'] for ref in (node.get('source_refs') or [])
                if isinstance(ref, dict)]
        self.assertFalse({ref.get('evidence_id') for ref in refs} & stale_ids)
        node = next(item for item in graph['nodes'] if item['id'] == self.kp_id)
        self.assertFalse({ref.get('evidence_id') for ref in node['source_refs']} & stale_ids)

    def test_dictionary_local_evidence_ignores_retired_rows(self):
        # Shorter doc: retired lines 3-6 have no live row with the same locator.
        self._rescan('# 成本核算\n边际成本提高。')
        source_id = self._scalar(
            'SELECT source_id FROM knowledge_candidates WHERE id=?',
            (self._candidates()['成本核算']['id'],))
        conn = db.connect()
        try:
            stale = conn.execute(
                '''SELECT * FROM evidence e WHERE e.source_id=? AND e.stale=1
                     AND NOT EXISTS (SELECT 1 FROM evidence l WHERE l.source_id=e.source_id
                          AND l.stale=0 AND l.locator_type=e.locator_type
                          AND l.locator_start IS e.locator_start AND l.locator_end IS e.locator_end)
                   ORDER BY e.id LIMIT 1''',
                (source_id,)).fetchone()
            live = conn.execute(
                'SELECT * FROM evidence WHERE source_id=? AND stale=0 ORDER BY id LIMIT 1',
                (source_id,)).fetchone()
            self.assertIsNotNone(stale)
            self.assertIsNotNone(live)

            def item(row):
                return {'locator_type': row['locator_type'],
                        'locator_start': row['locator_start'],
                        'locator_end': row['locator_end']}

            self.assertIsNone(relation_suggestions._local_evidence(conn, source_id, item(stale)))
            matched = relation_suggestions._local_evidence(conn, source_id, item(live))
            self.assertIsNotNone(matched)
            self.assertEqual(matched['id'], live['id'])
        finally:
            conn.close()

    def test_contract_validation_rejects_stale_evidence(self):
        self._rescan(
            '# 成本核算\n边际成本提高。\n'
            '# 固定成本\n固定成本提高。\n'
            '# 机会成本\n机会成本提高。')
        stale_id = self._scalar('SELECT id FROM evidence WHERE stale=1 ORDER BY id LIMIT 1')
        live_id = self._scalar('SELECT id FROM evidence WHERE stale=0 ORDER BY id LIMIT 1')
        self.assertIsNotNone(stale_id)
        with self.assertRaises(ValueError) as caught:
            repo.update_node_contract(self.kp_id, {'evidence_ids': [stale_id]})
        message = str(caught.exception)
        self.assertIn('无效证据', message)
        self.assertIn(str(stale_id), message)
        # All ids retired is still a clear validation failure, not a silent pass.
        with self.assertRaises(ValueError):
            repo.update_node_contract(self.kp_id, {'evidence_ids': [stale_id, stale_id]})
        result = repo.update_node_contract(self.kp_id, {'evidence_ids': [live_id]})
        self.assertEqual(result['evidence_ids'], [live_id])

    def test_relation_candidates_with_stale_evidence_are_hidden(self):
        source_id = self._scalar('SELECT id FROM source_files LIMIT 1')
        live_id = self._scalar(
            "SELECT id FROM evidence WHERE source_id=? AND stale=0 AND content LIKE '%反映新增产量%'",
            (source_id,))

        def insert_candidate(evidence_id):
            conn = db.connect()
            try:
                with conn:
                    conn.execute(
                        '''INSERT INTO relation_candidates
                           (source_id,from_kp,to_kp,from_name,to_name,kind,evidence_id,
                            confidence,status,reason)
                           VALUES(?,?,?,?,?,'related',?,0.6,'pending','测试')''',
                        (source_id, self.kp_id, self.kp_id, '成本核算', '成本核算', evidence_id))
            finally:
                conn.close()

        insert_candidate(live_id)
        listed = repo.list_relation_candidates('会计学')
        self.assertEqual([item['evidence_id'] for item in listed], [live_id])

        self._rescan(
            '# 成本核算\n边际成本提高。\n'
            '# 固定成本\n固定成本提高。\n'
            '# 机会成本\n机会成本提高。')
        self.assertIn(live_id, self._stale_evidence_ids())
        self.assertEqual(repo.list_relation_candidates('会计学'), [])
        self.assertEqual(
            self._scalar('SELECT COUNT(*) FROM relation_candidates'), 1)

        new_live_id = self._scalar(
            'SELECT id FROM evidence WHERE source_id=? AND stale=0 ORDER BY id LIMIT 1',
            (source_id,))
        insert_candidate(new_live_id)
        listed = repo.list_relation_candidates('会计学')
        self.assertEqual([item['evidence_id'] for item in listed], [new_live_id])

    def test_vanished_pending_candidate_is_superseded_then_revived(self):
        removed = self._candidates()['固定成本']
        self.assertEqual(removed['status'], 'pending')

        self._rescan(
            '# 成本核算\n边际成本提高。\n'
            '# 机会成本\n机会成本提高。')
        self.assertNotIn('固定成本', self._candidates())
        row = self._query(
            'SELECT id,status,superseded_by FROM knowledge_candidates WHERE name=?',
            ('固定成本',))[0]
        self.assertEqual(row['id'], removed['id'])
        self.assertEqual(row['status'], 'superseded')
        self.assertEqual(row['superseded_by'], row['id'])
        reviews = self._query(
            'SELECT action,note,superseded_at FROM candidate_reviews WHERE candidate_id=?',
            (removed['id'],))
        superseded = [item for item in reviews if item['action'] == 'superseded']
        self.assertEqual(len(superseded), 1)
        self.assertTrue(superseded[0]['superseded_at'])
        self.assertIn('快照', superseded[0]['note'])
        with self.assertRaises(ValueError) as caught:
            sources.confirm_candidate(removed['id'])
        self.assertIn('失效', str(caught.exception))
        with self.assertRaises(ValueError):
            sources.review_candidate(removed['id'], 'rejected')

        self._rescan(
            '# 成本核算\n边际成本提高。\n'
            '# 固定成本\n固定成本重新出现。\n'
            '# 机会成本\n机会成本提高。')
        revived = self._candidates()['固定成本']
        self.assertEqual(revived['id'], removed['id'])
        self.assertEqual(revived['status'], 'pending')
        self.assertIsNone(self._scalar(
            'SELECT superseded_by FROM knowledge_candidates WHERE id=?', (removed['id'],)))
        confirmed = sources.confirm_candidate(removed['id'])
        self.assertEqual(confirmed['status'], 'confirmed')
        self.assertEqual(self._candidates()['固定成本']['status'], 'confirmed')

    def test_confirm_uses_live_evidence_when_candidate_links_are_stale(self):
        self._rescan(
            '# 成本核算\n边际成本提高。\n'
            '# 固定成本\n固定成本提高。\n'
            '# 机会成本\n机会成本提高。')
        candidate = self._candidates()['机会成本']
        self.assertEqual(candidate['status'], 'pending')
        source_id = self._scalar(
            'SELECT source_id FROM knowledge_candidates WHERE id=?', (candidate['id'],))
        stale_id = self._scalar(
            'SELECT id FROM evidence WHERE source_id=? AND stale=1 ORDER BY id LIMIT 1',
            (source_id,))
        self.assertIsNotNone(stale_id)
        conn = db.connect()
        try:
            with conn:
                conn.execute(
                    'INSERT INTO knowledge_candidate_evidence(candidate_id,evidence_id) VALUES(?,?)',
                    (candidate['id'], stale_id))
        finally:
            conn.close()
        confirmed = sources.confirm_candidate(candidate['id'])
        links = self._query(
            '''SELECT e.id,e.stale FROM kp_evidence ke JOIN evidence e ON e.id=ke.evidence_id
               WHERE ke.kp_id=?''', (confirmed['target_kp_id'],))
        self.assertTrue(links)
        self.assertTrue(all(item['stale'] == 0 for item in links))
        self.assertNotIn(stale_id, {item['id'] for item in links})

    def test_concept_candidate_without_snapshot_is_not_superseded(self):
        concept_id = self._insert_concept_candidate('复合人物画像')
        self.assertIn('复合人物画像', self._candidates())

        self._rescan(
            '# 成本核算\n边际成本提高。\n'
            '# 机会成本\n机会成本提高。')
        # Heading-derived 固定成本 vanished; the concept candidate did not.
        self.assertNotIn('固定成本', self._candidates())
        row = self._query(
            'SELECT status,superseded_by,snapshot_id FROM knowledge_candidates WHERE id=?',
            (concept_id,))[0]
        self.assertIsNone(row['snapshot_id'])
        self.assertEqual(row['status'], 'pending')
        self.assertIsNone(row['superseded_by'])
        self.assertIn('复合人物画像', self._candidates())
        self.assertEqual(self._scalar(
            'SELECT COUNT(*) FROM candidate_reviews WHERE candidate_id=?', (concept_id,)), 0)
        # It stays confirmable after the rescan.
        self.assertEqual(sources.confirm_candidate(concept_id)['status'], 'confirmed')

    def test_concept_candidate_becomes_heading_derived_before_supersede(self):
        concept_id = self._insert_concept_candidate('边际分析')
        self.assertIsNone(self._scalar(
            'SELECT snapshot_id FROM knowledge_candidates WHERE id=?', (concept_id,)))

        self._rescan(
            '# 成本核算\n边际成本提高。\n'
            '# 固定成本\n固定成本提高。\n'
            '# 机会成本\n机会成本提高。\n'
            '# 边际分析\n边际分析是新增产量的增量分析。')
        row = self._query(
            'SELECT status,snapshot_id FROM knowledge_candidates WHERE id=?', (concept_id,))[0]
        self.assertEqual(row['status'], 'pending')
        self.assertIsNotNone(row['snapshot_id'])

        self._rescan(
            '# 成本核算\n边际成本提高。\n'
            '# 固定成本\n固定成本提高。\n'
            '# 机会成本\n机会成本提高。')
        row = self._query(
            'SELECT status,superseded_by FROM knowledge_candidates WHERE id=?', (concept_id,))[0]
        self.assertEqual(row['status'], 'superseded')
        self.assertEqual(row['superseded_by'], concept_id)
        actions = [item['action'] for item in self._query(
            'SELECT action FROM candidate_reviews WHERE candidate_id=?', (concept_id,))]
        self.assertIn('superseded', actions)
        self.assertNotIn('边际分析', self._candidates())

    def test_confirm_without_live_evidence_uses_empty_refs(self):
        self._rescan(
            '# 成本核算\n边际成本提高。\n'
            '# 固定成本\n固定成本提高。\n'
            '# 机会成本\n机会成本提高。')
        candidate = self._candidates()['机会成本']
        source_id = self._scalar(
            'SELECT source_id FROM knowledge_candidates WHERE id=?', (candidate['id'],))
        stale_id = self._scalar(
            'SELECT id FROM evidence WHERE source_id=? AND stale=1 ORDER BY id LIMIT 1',
            (source_id,))
        self.assertIsNotNone(stale_id)
        conn = db.connect()
        try:
            with conn:
                conn.execute(
                    'INSERT INTO knowledge_candidate_evidence(candidate_id,evidence_id) VALUES(?,?)',
                    (candidate['id'], stale_id))
                # No live extraction is left for this source.
                conn.execute('UPDATE evidence SET stale=1 WHERE source_id=?', (source_id,))
        finally:
            conn.close()

        confirmed = sources.confirm_candidate(candidate['id'])
        self.assertEqual(confirmed['status'], 'confirmed')
        kp_id = confirmed['target_kp_id']
        self.assertEqual(self._scalar('SELECT COUNT(*) FROM kp_evidence WHERE kp_id=?', (kp_id,)), 0)
        meta = self._query(
            'SELECT source_refs_json FROM knowledge_node_meta WHERE kp_id=?', (kp_id,))[0]
        self.assertEqual(json.loads(meta['source_refs_json']), [])


if __name__ == '__main__':
    unittest.main()
