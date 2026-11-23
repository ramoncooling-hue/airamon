"""Model output must not escape the frozen domain or evidence boundary."""

import json
from unittest.mock import patch

import pytest

from app.memory import domain_profiles, wiki


def response(**changes):
    item = {'name': 'Source concept', 'kp_type': 'concept', 'evidence_refs': [1]}
    item.update(changes)
    return json.dumps({'summary': 'Summary', 'concepts': [item]})


@pytest.mark.parametrize('changes', [
    {'kp_type': 'invented_type'}, {'kp_type': None}, {'kp_type': ['concept']},
    {'evidence_refs': []}, {'evidence_refs': [True]}, {'evidence_refs': [0]},
    {'evidence_refs': [2]}, {'name': ['invented name']},
])
def test_invalid_model_candidates_fail_closed(changes):
    with pytest.raises(ValueError):
        wiki.parse_assisted_response(response(**changes), evidence_count=1)


def test_model_cannot_self_approve():
    result = wiki.parse_assisted_response(response(status='confirmed'), evidence_count=1)
    assert result['concepts'][0]['status'] == 'pending_review'


def test_profile_constraints_apply_before_normalization():
    profile = domain_profiles.profile_for_course('', 'WorkPal')
    with pytest.raises(ValueError):
        wiki.parse_assisted_response(response(kp_type='exam_point'), evidence_count=1, profile=profile)


def test_no_silent_candidate_truncation():
    raw = json.dumps({'concepts': [json.loads(response())['concepts'][0]] * 41})
    with pytest.raises(ValueError):
        wiki.parse_assisted_response(raw, evidence_count=1)


def test_invalid_response_does_not_write_artifacts(tmp_path):
    with patch.object(wiki.llm, 'chat', return_value=response(kp_type='exam_point')):
        with pytest.raises(ValueError):
            wiki.assist_draft(
                course_dir=tmp_path, course='MBA plans', domain='WorkPal',
                source_path='input.md', source_sha256='abc', content='Evidence', headings=[],
                evidence=[{'text': 'Evidence', 'locator_type': 'line', 'locator_start': 1}],
            )
    assert list(tmp_path.iterdir()) == []


def test_prompt_uses_selected_domain_and_all_numbered_evidence():
    profile = domain_profiles.profile_for_course('', 'LifePal')
    messages = wiki._assistant_prompt(
        course='MBA plans', source_path='source.md', content='Evidence', headings=[],
        evidence=[{'text': 'Evidence', 'locator_type': 'line', 'locator_start': 1}],
        profile=profile,
    )
    payload = json.loads(messages[1]['content'])
    assert payload['domain_contract']['ontology_version'] == 'personal@1.0.0'
    assert payload['domain_contract']['id'] == 'life'
    assert 'exam_point' not in payload['domain_contract']['node_types']


def test_many_short_evidence_blocks_are_batched_without_hidden_references(tmp_path):
    counts = []

    def model(tier, messages, **kwargs):
        payload = json.loads(messages[1]['content'])
        count = len(payload['evidence'].splitlines())
        counts.append(count)
        return response(name=f'Concept {len(counts)}', evidence_refs=[count])

    evidence = [{'text': 'x', 'locator_type': 'line', 'locator_start': i} for i in range(1, 122)]
    with patch.object(wiki.llm, 'chat', side_effect=model):
        result = wiki.assist_draft(
            course_dir=tmp_path, course='Course', source_path='input.md', source_sha256='abc',
            content='\n'.join('x' for _ in evidence), headings=[], evidence=evidence,
        )
    assert counts == [120, 1]
    assert [c['evidence_refs'] for c in result['concepts']] == [[120], [121]]
    assert '证据121：' in (tmp_path / 'notes' / 'input.md').read_text(encoding='utf-8')
    assert json.loads((tmp_path / 'dictionary' / 'input.json').read_text())['ontology_version'] == 'personal@1.0.0'


def test_no_evidence_fails_before_calling_model(tmp_path):
    with patch.object(wiki.llm, 'chat') as model:
        with pytest.raises(ValueError):
            wiki.assist_draft(
                course_dir=tmp_path, course='Course', source_path='input.md', source_sha256='abc',
                content='Text without locations', headings=[], evidence=[{'text': ''}],
            )
    model.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_multi_batch_candidates_are_not_silently_limited_to_120(tmp_path):
    batch = 0

    def model(*args, **kwargs):
        nonlocal batch
        batch += 1
        return json.dumps({'concepts': [
            {'name': f'Concept {batch}-{i}', 'kp_type': 'concept', 'evidence_refs': [1]}
            for i in range(40)
        ]})

    evidence = [{'text': 'x' * 13000, 'locator_type': 'line', 'locator_start': i} for i in range(4)]
    with patch.object(wiki.llm, 'chat', side_effect=model):
        result = wiki.assist_draft(
            course_dir=tmp_path, course='Course', source_path='input.md', source_sha256='abc',
            content='\n'.join(e['text'] for e in evidence), headings=[], evidence=evidence,
        )
    assert result['candidate_count'] == 160


def test_same_name_with_different_claims_blocks_silent_overwrite(tmp_path):
    calls = 0

    def model(*args, **kwargs):
        nonlocal calls
        calls += 1
        return json.dumps({'concepts': [{
            'name': 'Revenue', 'kp_type': 'metric',
            'gist': '2021: 100' if calls == 1 else '2022: 200',
            'evidence_refs': [1],
        }]})

    evidence = [
        {'text': 'x' * 13000, 'locator_type': 'line', 'locator_start': 1},
        {'text': 'y' * 13000, 'locator_type': 'line', 'locator_start': 2},
    ]
    with patch.object(wiki.llm, 'chat', side_effect=model):
        with pytest.raises(ValueError, match='同名.*不同'):
            wiki.assist_draft(
                course_dir=tmp_path, course='Course', source_path='input.md', source_sha256='abc',
                content='x\ny', headings=[], evidence=evidence,
            )
    assert list(tmp_path.iterdir()) == []


def test_identical_claims_can_combine_evidence(tmp_path):
    evidence = [{'text': 'x' * 13000, 'locator_type': 'line', 'locator_start': i} for i in range(2)]
    with patch.object(wiki.llm, 'chat', return_value=response(gist='Same claim')):
        result = wiki.assist_draft(
            course_dir=tmp_path, course='Course', source_path='input.md', source_sha256='abc',
            content='x\nx', headings=[], evidence=evidence,
        )
    assert result['candidate_count'] == 1
    assert result['concepts'][0]['evidence_refs'] == [1, 2]
