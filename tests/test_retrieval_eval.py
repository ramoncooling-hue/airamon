import json
from pathlib import Path
import tempfile
import unittest
import os
import subprocess
import sys
from unittest.mock import patch

from app.memory import retrieval_eval


class RetrievalEvalTests(unittest.TestCase):
    def test_hybrid_callback_preserves_positional_api_and_candidate_decision(self):
        expected = [{'kind': 'kp', 'course_name': 'A', 'name': name} for name in ('x', 'y')]
        cases = [{'query': 'x', 'course': 'A', 'expected': expected}]
        calls = []

        def hybrid(query, course):
            calls.append((query, course))
            return {'status': 'ready', 'decision': 'candidates',
                    'hits': [dict(expected[0], gist='private body', rrf_score=.03), expected[1]]}

        report = retrieval_eval.evaluate(cases, lambda q, c: [], None, 1, hybrid_search=hybrid)
        self.assertEqual(calls, [('x', 'A')])
        self.assertEqual(report['cases'][0]['hybrid']['decision'], 'candidates')
        summary = report['summary']['hybrid']
        self.assertEqual(summary['recall_at_1'], .5)
        self.assertEqual(summary['recall_at_k'], .5)
        self.assertEqual(summary['hit_at_1'], 1)
        self.assertEqual(summary['decision_counts'], {'candidates': 1})
        self.assertEqual(summary['status_counts'], {'ready': 1})
        self.assertNotIn('private body', json.dumps(report))
        self.assertNotIn('rrf_score', json.dumps(report))
        self.assertEqual(retrieval_eval.evaluate(cases, lambda q, c: [], None, 1)
                         ['summary']['hybrid']['status'], 'not_run')
        with self.assertRaises(TypeError):
            retrieval_eval.evaluate(cases, lambda q, c: [], None, 1, hybrid)

    def test_positive_and_negative_metrics_use_separate_denominators(self):
        x, y = [{'kind': 'kp', 'course_name': 'A', 'name': name} for name in ('x', 'y')]
        cases = [{'query': 'first', 'expected': [x, y]},
                 {'query': 'second', 'expected': [y]},
                 {'query': 'empty', 'expected': [x]},
                 {'query': 'negative-hit', 'expected': []},
                 {'query': 'negative-empty', 'expected': []}]
        responses = {'first': [x, x, y], 'second': [x, y], 'empty': [],
                     'negative-hit': [x], 'negative-empty': []}
        report = retrieval_eval.evaluate(cases, lambda q, c: responses[q], limit=2)
        summary = report['summary']['keyword']
        self.assertAlmostEqual(summary['recall_at_1'], 1 / 6)
        self.assertEqual(summary['recall_at_k'], .5)
        self.assertEqual(summary['mrr'], .5)
        self.assertEqual(summary['positive_without_results'], 1)
        self.assertEqual(summary['negative_with_results'], 1)
        self.assertEqual(summary['negative_without_results'], 1)
        self.assertEqual(summary['negative_with_results_rate'], .5)
        self.assertEqual(summary['negative_without_results_rate'], .5)

    def test_degraded_and_failed_negatives_never_count_as_successful_empty_results(self):
        cases = [{'query': query, 'expected': []} for query in ('empty', 'failed')]
        for failure in ({'status': 'degraded', 'decision': 'unavailable', 'hits': []},
                        {'status': 'degraded', 'decision': 'candidates', 'hits': [{'name': 'x'}]},
                        {'status': 'not_built', 'hits': []},
                        {'status': 'rebuild_required', 'hits': []},
                        {'status': 'ready', 'error': 'private error', 'hits': []},
                        {'status': 'ready', 'hits': None}):
            with self.subTest(failure=failure):
                report = retrieval_eval.evaluate(cases, lambda q, c: [], hybrid_search=lambda q, c:
                    {'status': 'ready', 'decision': 'no_candidates', 'hits': []} if q == 'empty' else failure)
                summary = report['summary']['hybrid']
                self.assertEqual(summary['status'], 'incomplete')
                self.assertEqual(summary['evaluated'], 1)
                self.assertEqual(summary['negative_cases'], 1)
                self.assertEqual(summary['negative_without_results'], 1)
                self.assertEqual(summary['negative_with_results'], 0)
                self.assertIsNone(summary['negative_without_results_rate'])
                self.assertIsNone(summary['negative_with_results_rate'])
                failed = report['cases'][1]['hybrid']
                status = failure['status'] if failure['status'] != 'ready' else 'error'
                self.assertEqual(failed['status'], status)
                self.assertEqual(summary['status_counts'], {'ready': 1, status: 1})
                if 'decision' in failure:
                    self.assertEqual(failed['decision'], failure['decision'])
                    self.assertEqual(summary['decision_counts'][failure['decision']], 1)
                self.assertNotIn('private error', json.dumps(report))

    def test_complete_all_negative_report_and_categories_are_applicable(self):
        cases = [{'query': 'empty', 'expected': []}, {'query': 'hit', 'expected': []}]
        report = retrieval_eval.evaluate(cases, lambda q, c: [], lambda q, c: [],
            hybrid_search=lambda q, c: {'status': 'ready',
                'decision': 'no_candidates' if q == 'empty' else 'candidates',
                'hits': [] if q == 'empty' else [{'name': 'x'}]})
        for metrics in report['summary'].values():
            self.assertEqual(metrics['status'], 'complete')
            for key in ('recall_at_1', 'recall_at_k', 'mrr', 'hit_at_1'):
                self.assertIsNone(metrics[key])
        # The script's category schema predates the new rate fields.
        report['by_category'] = {'negative': {'hybrid': {
            'cases': 2, 'total': 2, 'positive_cases': 0, 'negative_cases': 2,
            'negative_with_results': 1, 'recall_at_k': None, 'hit_at_1': None}}}
        rendered = retrieval_eval.format_report(report)
        self.assertIn('Recall@1', rendered)
        self.assertIn('负例有结果率', rendered)
        self.assertIn('负例零结果率', rendered)
        self.assertIn('不适用', rendered)
        self.assertNotIn('| 未评定 |', rendered)
        self.assertNotIn('incomplete', rendered)
        self.assertIn('- hybrid: ready', rendered)
        self.assertIn('decision=no_candidates', rendered)
        self.assertIn('decision=candidates', rendered)
        category = next(line for line in rendered.splitlines() if line.startswith('| negative |'))
        self.assertGreaterEqual(category.count('0.500'), 2)

    def test_report_preserves_degradation_and_does_not_call_it_rejection(self):
        report = retrieval_eval.evaluate([{'query': 'missing', 'expected': []}], lambda q, c: [],
            hybrid_search=lambda q, c: {'status': 'degraded', 'decision': 'unavailable', 'hits': []})
        rendered = retrieval_eval.format_report(report)
        self.assertIn('degraded', rendered)
        self.assertIn('decision=unavailable', rendered)
        hybrid = next(line for line in rendered.splitlines() if line.startswith('| hybrid |'))
        self.assertIn('未评定', hybrid)
        self.assertNotIn('不适用', hybrid)

    def test_direct_worker_also_uses_disposable_database(self):
        from scripts.evaluate_retrieval import ROOT
        with tempfile.TemporaryDirectory() as directory:
            selected = Path(directory) / 'personal'
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/evaluate_retrieval.py'), '--worker'],
                env=dict(os.environ, CYBERPAL_DATA_DIR=str(selected)), capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(selected.exists())
            self.assertTrue(json.loads(result.stdout)['metadata']['isolated'])

    def test_isolated_fixture_never_initializes_selected_personal_directory(self):
        from scripts.evaluate_retrieval import run
        with tempfile.TemporaryDirectory() as directory:
            selected = Path(directory) / 'personal'
            with patch.dict(os.environ, CYBERPAL_DATA_DIR=str(selected)):
                report = run()
            self.assertFalse(selected.exists())
        self.assertEqual(len(report['cases']), 42)
        self.assertEqual(report['summary']['vector']['status'], 'not_run')
        boundaries = [r for r in report['cases'] if r['id'] in ('wrong-course', 'workspace-boundary')]
        self.assertTrue(all(r['keyword']['returned'] == 0 for r in boundaries))
        self.assertEqual(report['by_category']['workspace']['keyword']['cases'], 1)
        self.assertEqual(report['by_category']['workspace']['keyword']['negative_cases'], 1)
        self.assertIsNone(report['by_category']['workspace']['keyword']['recall_at_k'])

    def test_evaluate_reports_keyword_and_vector_without_source_text(self):
        cases = [{'id': 'cost', 'query': '边际成本', 'course': '会计学',
                  'expected': [{'kind': 'kp', 'course_name': '会计学', 'name': '边际成本'}]}]
        result = retrieval_eval.evaluate(
            cases,
            lambda q, course: [{'kind': 'kp', 'course_name': course, 'name': '边际成本', 'gist': 'secret'}],
            lambda q, course: {'hits': [{'kind': 'kp', 'course_name': course, 'name': '边际成本', 'gist': 'secret'}]},
        )
        self.assertEqual(result['summary']['keyword']['recall_at_k'], 1.0)
        self.assertEqual(result['summary']['vector']['mrr'], 1.0)
        self.assertNotIn('secret', json.dumps(result))

    def test_loader_rejects_unbounded_or_malformed_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cases.json'
            path.write_text(json.dumps([{'query': 'x', 'expected': [{'kind': 'kp'}]}]), encoding='utf-8')
            with self.assertRaises(ValueError):
                retrieval_eval.load_cases(path)

    def test_report_marks_missing_vector_as_unavailable(self):
        cases = [{'id': 'x', 'query': 'x', 'expected': [{'kind':'kp','course_name':'A','name':'x'}]}]
        result = retrieval_eval.evaluate(cases, lambda q, c: [])
        self.assertIsNone(result['summary']['vector']['recall_at_k'])
        self.assertEqual(result['summary']['vector']['status'], 'not_run')

    def test_unbuilt_and_failed_vector_requests_are_not_zero_recall(self):
        cases = [{'id': 'x', 'query': 'x', 'expected': [{'kind':'kp','course_name':'A','name':'x'}]}]
        for response in [{'status': 'not_built', 'hits': []}, {'error':'private-secret'},
                         {'status':'rebuild_required', 'hits':[]}]:
            result = retrieval_eval.evaluate(cases, lambda q,c: [], lambda q,c: response)
            self.assertIsNone(result['summary']['vector']['recall_at_k'])
            self.assertEqual(result['summary']['vector']['status'], 'incomplete')
            self.assertNotIn('private-secret', json.dumps(result))

    def test_recall_deduplicates_hits_and_mrr_uses_first_rank(self):
        expected = [{'kind':'kp','course_name':'A','name':name} for name in ('x','y')]
        result = retrieval_eval.evaluate([{'id':'x','query':'x','expected':expected}],
            lambda q,c: [dict(expected[0], name='wrong'), expected[0], expected[0]], limit=3)
        self.assertEqual(result['summary']['keyword']['recall_at_k'], .5)
        self.assertEqual(result['summary']['keyword']['mrr'], .5)
        self.assertEqual(result['summary']['keyword']['hit_at_1'], 0)
        self.assertGreaterEqual(result['summary']['keyword']['elapsed_seconds'], 0)

    def test_unanswerable_questions_are_separate_from_positive_recall(self):
        cases = [{'id':'none','query':'not in corpus','expected':[]}]
        result = retrieval_eval.evaluate(cases, lambda q,c: [{'name':'x'}])
        self.assertIsNone(result['summary']['keyword']['recall_at_k'])
        self.assertEqual(result['summary']['keyword']['negative_with_results'], 1)

    def test_validation_rejects_invalid_scope_duplicate_ids_and_empty_cases(self):
        for value in [[], [{'query':'x','course':42,'expected':[]}],
                      [{'id':'same','query':'x','expected':[]}, {'id':'same','query':'y','expected':[]}]]:
            with self.assertRaises(ValueError):
                retrieval_eval.validate_cases(value)

    def test_error_on_one_case_invalidates_aggregate_comparison(self):
        cases = [{'id':x,'query':x,'expected':[{'kind':'kp','course_name':'A','name':'x'}]} for x in ('x','y')]
        def search(q,c):
            if q == 'y': raise RuntimeError('private body')
            return {'status':'ready','hits':[{'kind':'kp','course_name':'A','name':'x'}]}
        result = retrieval_eval.evaluate(cases, lambda q,c: [], search)
        self.assertEqual(result['summary']['vector']['evaluated'], 1)
        self.assertIsNone(result['summary']['vector']['recall_at_k'])
        self.assertNotIn('private body', json.dumps(result))
