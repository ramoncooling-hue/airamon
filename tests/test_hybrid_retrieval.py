import unittest
from unittest.mock import patch

from app.memory import hybrid_retrieval as hybrid


def hit(identifier=1, **values):
    return {'kind':'kp', 'id':identifier, 'course_name':'A', 'name':f'概念{identifier}',
            'text':'当前正文', **values}


class HybridTests(unittest.TestCase):
    def test_rrf_deduplicates_and_does_not_accept_weak_keyword_alone(self):
        current = [hit(1), hit(2), hit(3)]
        result = hybrid.fuse('问题', [hit(1), hit(1), hit(2)],
            {'status':'ready', 'hits':[hit(2, distance=.2), hit(3, distance=.3), hit(1, distance=.8)]},
            current, min_similarity=.6)
        self.assertEqual([r['id'] for r in result['hits']], [2,3])
        self.assertEqual(result['decision'], 'candidates')

    def test_only_exact_title_bypasses_semantic_gate(self):
        for query, expected in [('概念1', 1), ('概念1在讲义第99页是什么', 0)]:
            result = hybrid.fuse(query, [hit()], {'status':'ready','hits':[hit(distance=.8)]},
                                 [hit()], min_similarity=.6)
            self.assertEqual(len(result['hits']), expected)

    def test_identity_uses_kind_and_id_not_name(self):
        rows = [hit(1, name='同名'), hit(2, name='同名'), hit(1, kind='evidence', name='同名')]
        result = hybrid.fuse('同名', rows, {'status':'ready','hits':[]}, rows, min_similarity=.6)
        self.assertEqual(len(result['hits']), 3)

    def test_removed_or_changed_rows_are_not_returned_from_old_candidates(self):
        original = hit()
        changed = hit(name='更名')
        result = hybrid.fuse('概念1', [original], {'status':'ready','hits':[hit(distance=.1)]},
                             [changed], min_similarity=.6)
        self.assertEqual(result['hits'], [])
        self.assertEqual(hybrid.fuse('概念1', [original], {'status':'ready','hits':[]}, [], min_similarity=.6)['hits'], [])

    def test_unavailable_is_degraded_and_not_a_successful_abstention(self):
        for response in ({'status':'not_built','hits':[]}, {'error':'secret'}):
            result = hybrid.fuse('概念1', [hit()], response, [hit()], min_similarity=.6)
            self.assertEqual(result['status'], 'degraded')
            self.assertNotIn('secret', str(result))

    def test_invalid_threshold_and_invalid_distances(self):
        for value in (None, -1, 2, float('nan'), True):
            with self.assertRaises(ValueError):
                hybrid.fuse('q', [], {'status':'ready','hits':[]}, [], min_similarity=value)
        for value in (float('nan'), float('inf'), -.5, True, '0.2'):
            result = hybrid.fuse('q', [], {'status':'ready','hits':[hit(distance=value)]}, [hit()], min_similarity=.6)
            self.assertEqual(result['hits'], [])
            self.assertEqual(result['status'], 'degraded')
            self.assertEqual(result['decision'], 'unavailable')

    def test_search_hydrates_after_both_paths_and_uses_scope(self):
        with patch.object(hybrid, '_keyword', return_value=[hit()]) as keyword, \
             patch.object(hybrid.vector_index, 'search', return_value={'status':'ready','hits':[hit(distance=.1)]}) as vector, \
             patch.object(hybrid.vector_index, '_corpus', return_value=[]) as corpus:
            result = hybrid.search('概念1', 'study', object(), 'A', min_similarity=.6)
            self.assertEqual(result['hits'], [])
            corpus.assert_called_once_with('study', 'A')
            self.assertEqual(keyword.call_args.args[1:3], ('study','A'))
            self.assertEqual(vector.call_args.args[1], 'study')

    def test_keyword_failure_preserves_vector_candidates_as_degraded(self):
        with patch.object(hybrid, '_keyword', side_effect=RuntimeError('private error')), \
             patch.object(hybrid.vector_index, 'search', return_value={'status':'ready','hits':[hit(distance=.1)]}), \
             patch.object(hybrid.vector_index, '_corpus', return_value=[hit()]):
            result = hybrid.search('question', 'study', object(), min_similarity=.6)
        self.assertEqual(result['status'], 'degraded')
        self.assertEqual(result['decision'], 'candidates')
        self.assertEqual(len(result['hits']), 1)
        self.assertNotIn('private error', str(result))

