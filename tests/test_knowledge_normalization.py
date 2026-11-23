"""T2.7/O-TEST-2: memory/knowledge.py 契约常量与 normalize_candidate 边界。

knowledge.py 是模型候选进入 kp 前的规范化闸门：常量是跨模块契约，
normalize_candidate/validate_relation 负责白名单与截断。本文件冻结这些边界。
"""

import pytest

from app.memory import knowledge

EXPECTED_NODE_TYPES = {
    'concept', 'model', 'method', 'case', 'exam_point', 'material',
    'entity', 'process', 'metric', 'event', 'decision', 'rule',
}
EXPECTED_RELATION_KINDS = {
    'pre', 'related', 'part_of', 'supports', 'example_of', 'causes',
    'contrasts', 'bridge',
}
EXPECTED_STATUSES = {
    'discovered', 'extracted', 'deduped', 'pending', 'pending_review',
    'confirmed', 'merged', 'rejected', 'stale',
}
CONTRACT_FIELDS = {'schema_version', 'name', 'kp_type', 'chapter', 'gist',
                   'detail', 'trap', 'source', 'confidence', 'status'}


def test_contract_constants_are_frozen():
    assert knowledge.SCHEMA_VERSION == 'cyberpal.knowledge.v1'
    assert knowledge.NODE_TYPES == EXPECTED_NODE_TYPES
    assert knowledge.RELATION_KINDS == EXPECTED_RELATION_KINDS
    assert knowledge.STATUSES == EXPECTED_STATUSES


def test_normalize_returns_exactly_the_contract_fields():
    result = knowledge.normalize_candidate({'name': '边际成本'}, source='教材')
    assert set(result) == CONTRACT_FIELDS
    assert result['schema_version'] == knowledge.SCHEMA_VERSION
    assert result['source'] == '教材'


def test_name_whitespace_is_collapsed_and_truncated():
    assert knowledge.normalize_candidate(
        {'name': '  边际   成本  '}, source='s')['name'] == '边际 成本'
    assert len(knowledge.normalize_candidate(
        {'name': 'x' * 300}, source='s')['name']) == 180


@pytest.mark.parametrize('name', ['', '   ', None])
def test_empty_name_is_rejected(name):
    with pytest.raises(ValueError, match='知识点名称不能为空'):
        knowledge.normalize_candidate({'name': name}, source='s')


@pytest.mark.parametrize('raw_type', ['mindmap', 'Concept', 'concept ', None, ''])
def test_unknown_node_types_fall_back_to_material(raw_type):
    result = knowledge.normalize_candidate(
        {'name': 'n', 'kp_type': raw_type}, source='s')
    assert result['kp_type'] == 'material'


@pytest.mark.parametrize('node_type', sorted(EXPECTED_NODE_TYPES))
def test_valid_node_types_are_preserved(node_type):
    result = knowledge.normalize_candidate(
        {'name': 'n', 'kp_type': node_type}, source='s')
    assert result['kp_type'] == node_type


@pytest.mark.parametrize('raw,expected', [
    (2, 1.0), (-3, 0.0), ('0.3', 0.3), ('abc', 0.55), (None, 0.55), ('', 0.55),
])
def test_confidence_is_clamped_and_defaults(raw, expected):
    result = knowledge.normalize_candidate(
        {'name': 'n', 'confidence': raw}, source='s')
    assert result['confidence'] == expected


def test_text_fields_have_fallbacks_and_limits():
    fallback = knowledge.normalize_candidate({'name': '知识点'}, source='材料')
    assert fallback['chapter'] == '来源材料'
    assert fallback['gist'] == '知识点'
    assert fallback['detail'] == ''
    assert fallback['trap'] == ''

    long = knowledge.normalize_candidate({
        'name': 'n',
        'chapter': 'c' * 300,
        'gist': 'g' * 1200,
        'detail': 'd' * 5000,
        'trap': 't' * 1200,
    }, source='s')
    assert len(long['chapter']) == 180
    assert len(long['gist']) == 1000
    assert len(long['detail']) == 4000
    assert len(long['trap']) == 1000


@pytest.mark.parametrize('status', sorted(EXPECTED_STATUSES - {'pending'}))
def test_known_statuses_are_preserved(status):
    result = knowledge.normalize_candidate(
        {'name': 'n', 'status': status}, source='s')
    assert result['status'] == status


@pytest.mark.parametrize('status', ['weird', 5, None])
def test_unknown_status_falls_back_to_pending(status):
    result = knowledge.normalize_candidate(
        {'name': 'n', 'status': status}, source='s')
    assert result['status'] == 'pending'


@pytest.mark.parametrize('kind', sorted(EXPECTED_RELATION_KINDS))
def test_validate_relation_accepts_known_kinds(kind):
    assert knowledge.validate_relation(kind) == kind


@pytest.mark.parametrize('kind', ['unknown', 'PRE', '', None, 'pre '])
def test_validate_relation_rejects_unknown_kinds(kind):
    with pytest.raises(ValueError, match='不支持的知识关系'):
        knowledge.validate_relation(kind)
