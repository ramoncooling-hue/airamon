import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import evaluate_answer_support as evaluation


def test_model_evaluation_is_explicit_and_does_not_initialize_data(tmp_path):
    personal = tmp_path / 'personal'
    output = tmp_path / 'report'
    result = subprocess.run([sys.executable, str(Path(evaluation.__file__)), '--output', str(output)],
        env=dict(os.environ, CYBERPAL_DATA_DIR=str(personal)), capture_output=True, text=True, timeout=5)
    assert result.returncode == 1
    assert '未运行' in result.stdout
    assert not output.exists() and not personal.exists()


def test_missing_configuration_does_not_initialize_database(tmp_path):
    personal = tmp_path / 'personal'
    result = subprocess.run([sys.executable, str(Path(evaluation.__file__)), '--model-check',
        '--output', str(tmp_path / 'output')], env=dict(os.environ, CYBERPAL_DATA_DIR=str(personal)),
        capture_output=True, text=True, timeout=5)
    assert result.returncode == 1
    assert '未评定' in result.stdout
    assert not personal.exists()


def test_failures_are_not_counted_as_correct_abstentions():
    cases = [{'id':'negative','query':'where','documents':[{'id':'a','text':'unrelated'}], 'expected':'insufficient'}]
    report = evaluation.evaluate(cases, lambda messages: 'invalid')
    assert report['summary']['status'] == 'incomplete'
    assert report['summary']['decision_accuracy'] is None
    assert report['summary']['false_support_rate'] is None


def test_deterministic_no_evidence_is_not_counted_as_model_evaluation():
    cases = [{'id':'empty','query':'where','documents':[], 'expected':'insufficient'}]
    report = evaluation.evaluate(cases, lambda messages: 'must not run')
    summary = report['summary']
    assert summary['status'] == 'incomplete'
    assert summary['evaluated'] == 0
    assert summary['deterministic_cases'] == 1
    assert summary['insufficient_cases'] == 1
    assert summary['insufficient_evaluated'] == 0
    assert summary['decision_accuracy'] is None


def test_failed_expected_negative_remains_in_negative_denominator():
    cases = [{'id':'missing','query':'where','documents':[{'id':'a','text':'unrelated'}],
              'expected':'insufficient'}]
    report = evaluation.evaluate(cases, lambda messages: 'invalid')
    summary = report['summary']
    assert summary['status'] == 'incomplete'
    assert summary['insufficient_cases'] == 1
    assert summary['insufficient_evaluated'] == 0
    assert summary['insufficient_marked_supported'] == 0
    assert summary['false_support_rate'] is None
    assert summary['evaluated_decision_accuracy'] is None


def test_metrics_measure_decisions_not_just_quotes():
    cases = [{'id':str(i),'query':'where','documents':[{'id':'a','text':'100万元'}], 'expected':expected}
             for i, expected in enumerate(['insufficient','supported'])]
    def complete(messages):
        p = json.loads(messages[-1]['content'])['passages'][0]
        return json.dumps({'decision':'supported','claims':[{'text':'100万元', 'citations':[
            {'passage_id':p['id'], 'quote':'100万元'}]}], 'missing':[]})
    report = evaluation.evaluate(cases, complete)
    assert report['summary']['decision_accuracy'] == .5
    assert report['summary']['evaluated_decision_accuracy'] == .5
    assert report['summary']['false_support_rate'] == 1


def test_holdout_loader_accepts_only_synthetic_fixture_files():
    holdout = Path(evaluation.__file__).resolve().parents[1] / 'tests/fixtures/answer_support_holdout_zh.json'
    cases = evaluation.load_cases(holdout)
    assert len(cases) >= 6
    assert len({case['id'] for case in cases}) == len(cases)
    assert all(case['expected'] in {'supported', 'partial', 'insufficient', 'conflict'} for case in cases)


def test_holdout_loader_rejects_files_outside_fixture_directory(tmp_path):
    private = tmp_path / 'private.json'
    private.write_text('[]', encoding='utf-8')
    try:
        evaluation.load_cases(private)
    except ValueError as exc:
        assert 'tests/fixtures' in str(exc)
    else:
        raise AssertionError('external case files must not be sent to the model')


def test_unregistered_files_inside_fixtures_are_not_sent(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation, 'FIXTURES_DIR', tmp_path)
    unreviewed = tmp_path / 'unreviewed.json'
    unreviewed.write_text(json.dumps([{
        'id':'unreviewed','query':'q', 'documents':[], 'expected':'insufficient'
    }]))
    with pytest.raises(ValueError, match='registered'):
        evaluation.load_cases(unreviewed)


def test_report_separates_structured_responses_valid_judgments_and_degradation():
    cases = [
        {'id':'valid','query':'收入是多少？','documents':[{'id':'a','text':'收入为100元。'}], 'expected':'supported'},
        {'id':'invalid-shape','query':'预算是多少？','documents':[{'id':'b','text':'预算为20元。'}], 'expected':'supported'},
        {'id':'malformed','query':'预算是多少？','documents':[{'id':'b','text':'预算为20元。'}], 'expected':'supported'},
        {'id':'empty','query':'地点在哪里？','documents':[], 'expected':'insufficient'},
    ]

    budget_calls = 0

    def complete(messages):
        nonlocal budget_calls
        if '预算' in messages[-1]['content']:
            budget_calls += 1
            if budget_calls == 1:
                return json.dumps({'decision':'unsupported-schema'})
            return 'not-json'
        packet = json.loads(messages[-1]['content'])
        passage = packet['passages'][0]
        quote = '收入为100元。'
        return json.dumps({'decision':'supported','claims':[{'text':quote,'citations':[
            {'passage_id':passage['id'],'quote':quote}]}],'missing':[]})

    summary = evaluation.evaluate(cases, complete)['summary']
    assert summary['model_attempted'] == 3
    assert summary['structured_responses'] == 2
    assert summary['structured_response_rate'] == 2 / 3
    assert summary['valid_judgments'] == 1
    assert summary['valid_judgment_rate'] == 1 / 3
    assert summary['degraded'] == 2
    assert summary['degraded_reasons'] == {'contract_validation': 1, 'json_parse': 1}


def test_explicit_evaluation_passes_total_budget_without_global_mutation(tmp_path, monkeypatch):
    from app.agent import llm
    monkeypatch.setattr(llm, 'available', lambda tier: True)
    calls = []

    def complete(tier, messages, **kwargs):
        calls.append(kwargs)
        raise llm.LLMError('private', code='timeout')

    monkeypatch.setattr(llm, 'chat', complete)
    monkeypatch.setattr(evaluation, 'load_cases', lambda path: [
        {'id': 'synthetic', 'query': 'q', 'documents': [{'id': 'x', 'text': 'text'}],
         'expected': 'insufficient'}])
    monkeypatch.setattr(sys, 'argv', ['evaluate', '--model-check', '--output', str(tmp_path / 'report')])
    previous = llm.TIMEOUT
    assert evaluation.main() == 1
    assert llm.TIMEOUT == previous
    assert calls == [{'temperature': 0, 'response_format': {'type': 'json_object'}, 'timeout_seconds': 30}]
    report = json.loads((tmp_path / 'report/report.json').read_text())
    assert report['summary']['failure_codes'] == {'timeout': 1}
