"""Frozen personal ontology and side-effect-free mapping validation."""

import copy
import importlib
import json
import socket
import sqlite3

import pytest


def ontology_module():
    return importlib.import_module('app.memory.ontology')


def mapping():
    return {
        'ontology_version': 'personal@1.0.0',
        'profile_id': 'work', 'profile_version': '1.0.0',
        'branch_keys': ['work.industry_knowledge'],
        'node_types': ['method'], 'relation_kinds': ['supports'],
        'field_mappings': {'title': 'name', 'body': 'gist'},
        'evidence_required': True, 'sample_refs': ['sample-1'],
        'model_id': 'test-model', 'prompt_version': 'mapping-v1',
    }


def test_contract_has_frozen_counts_and_stable_parent_keys():
    ontology = ontology_module()
    contract = ontology.public_contract()
    assert ontology.ONTOLOGY_VERSION == contract['ontology_version'] == 'personal@1.0.0'
    assert contract['root']['key'] == 'personal'
    assert {item['key'] for item in contract['trunks']} == {'work', 'study', 'life'}
    assert all(item['parent_key'] == 'personal' for item in contract['trunks'])
    assert {key: sum(branch['parent_key'] == key for branch in contract['branches'])
            for key in ('work', 'study', 'life')} == {'work': 7, 'study': 6, 'life': 10}
    assert len({item['key'] for item in contract['branches']}) == 23
    assert any(item['key'] == 'work.industry_knowledge' for item in contract['branches'])
    assert any(item['key'] == 'study.knowledge_understanding' for item in contract['branches'])
    assert len(contract['business_groups']) == 6
    assert {item['key'] for item in contract['governance_records']} == {
        'source', 'snapshot', 'evidence', 'review',
    }
    assert contract['mapping_policy']['branch_membership_grants_access'] is False
    assert contract['mapping_policy']['validation_grants_approval'] is False
    assert json.loads(json.dumps(contract)) == contract


def test_registry_and_schema_are_defensive_copies():
    ontology = ontology_module()
    contract = ontology.public_contract()
    original = copy.deepcopy(contract)
    contract['root']['key'] = 'changed'
    contract['trunks'][0]['key'] = 'changed'
    contract['branches'][0]['key'] = 'changed'
    contract['business_groups'].clear()
    contract['governance_records'].clear()
    contract['core_records'].clear()
    contract['mapping_policy']['allowed_target_fields'].clear()
    assert ontology.public_contract() == original
    schema = ontology.mapping_json_schema()
    original_schema = copy.deepcopy(schema)
    schema['properties'].clear()
    assert ontology.mapping_json_schema() == original_schema


def test_core_records_register_semantic_responsibilities_without_extending_node_types():
    from app.memory.knowledge import NODE_TYPES

    before = set(NODE_TYPES)
    contract = ontology_module().public_contract()
    records = {item['key']: item for item in contract['core_records']}
    assert len(records) == len(contract['core_records']) == 8
    assert set(records) == {
        'source', 'snapshot', 'evidence', 'entity', 'concept',
        'statement', 'personal_state', 'review',
    }
    assert all(item['description'].strip() for item in records.values())
    assert contract['mapping_policy']['core_records_are_node_types'] is False
    assert NODE_TYPES == before
    assert 'statement' not in NODE_TYPES and 'personal_state' not in NODE_TYPES


def test_valid_partial_mapping_is_repeatable_pending_review_and_independent():
    ontology = ontology_module()
    payload = mapping()
    payload['branch_keys'].append('work.projects_execution')
    before = copy.deepcopy(payload)
    result = ontology.validate_mapping(payload, workspace_kind='work')
    assert result == {**before, 'status': 'pending_review'}
    assert ontology.validate_mapping(payload, workspace_kind='work') == result
    assert payload == before
    assert json.loads(json.dumps(result)) == result
    result['branch_keys'].clear()
    result['field_mappings'].clear()
    assert payload == before


@pytest.mark.parametrize('field', list(mapping()))
def test_every_agreed_field_is_required(field):
    payload = mapping()
    del payload[field]
    with pytest.raises(ValueError):
        ontology_module().validate_mapping(payload, workspace_kind='work')


@pytest.mark.parametrize('field,value', [
    ('ontology_version', 'personal@2.0.0'), ('profile_id', 'missing'),
    ('profile_version', '1.0.1'), ('branch_keys', ['work.new_branch']),
    ('branch_keys', ['work']), ('branch_keys', ['personal']),
    ('branch_keys', ['work.industry_knowledge', 'study.knowledge_understanding']),
    ('branch_keys', ['study.knowledge_understanding']),
    ('node_types', ['invented']), ('node_types', ['exam_point']),
    ('relation_kinds', ['invented']), ('field_mappings', {'title': 'custom_attribute'}),
    ('field_mappings', {'title': 'status'}), ('field_mappings', {'title': 'approved'}),
    ('field_mappings', {'title': 'name', 'heading': 'name'}),
    ('field_mappings', {' title ': 'name', 'title': 'gist'}),
    ('field_mappings', {'': 'name'}), ('field_mappings', {'title': []}),
    ('field_mappings', {1: 'name'}), ('branch_keys', []), ('node_types', []),
    ('status', 'approved'), ('status', 'pending_review'), ('approved', True),
    ('workspace_kind', 'work'), ('workspace_key', 'workpal'),
    ('attributes', {'subtype': 'arbitrary'}), ('extra', 'unknown'),
])
def test_invalid_mapping_choices_and_extra_fields_are_rejected(field, value):
    payload = mapping()
    payload[field] = value
    with pytest.raises(ValueError):
        ontology_module().validate_mapping(payload, workspace_kind='work')


@pytest.mark.parametrize('field', ['ontology_version', 'profile_id', 'profile_version', 'model_id', 'prompt_version'])
@pytest.mark.parametrize('value', ['', ' \n ', None, 1, True, ['id']])
def test_ids_are_nonblank_strict_strings(field, value):
    payload = mapping()
    payload[field] = value
    with pytest.raises(ValueError):
        ontology_module().validate_mapping(payload, workspace_kind='work')


@pytest.mark.parametrize('value', [False, 0, 1, 'true', 'false', None, [], {}])
def test_evidence_policy_requires_literal_boolean_true(value):
    payload = mapping()
    payload['evidence_required'] = value
    with pytest.raises(ValueError):
        ontology_module().validate_mapping(payload, workspace_kind='work')


@pytest.mark.parametrize('field', ['branch_keys', 'node_types', 'relation_kinds', 'sample_refs'])
@pytest.mark.parametrize('value', ['text', ('method',), {0: 'method'}, [None], [1], [True], [' ']])
def test_mapping_lists_reject_type_coercion_and_blank_values(field, value):
    payload = mapping()
    payload[field] = value
    with pytest.raises(ValueError):
        ontology_module().validate_mapping(payload, workspace_kind='work')


@pytest.mark.parametrize('field', ['branch_keys', 'node_types', 'relation_kinds', 'sample_refs'])
def test_duplicate_list_values_are_rejected_after_normalization(field):
    payload = mapping()
    payload[field] = [payload[field][0], ' ' + payload[field][0] + ' ']
    with pytest.raises(ValueError):
        ontology_module().validate_mapping(payload, workspace_kind='work')


@pytest.mark.parametrize('workspace_kind', ['', 'WorkPal', 'WORK', 'general', ' work ', None, 1, True, []])
def test_workspace_kind_is_a_strict_trusted_host_argument(workspace_kind):
    with pytest.raises(ValueError):
        ontology_module().validate_mapping(mapping(), workspace_kind=workspace_kind)


@pytest.mark.parametrize('value', [None, [], 'mapping', 1, True])
def test_payload_must_be_a_json_object(value):
    with pytest.raises(ValueError):
        ontology_module().validate_mapping(value, workspace_kind='work')


@pytest.mark.parametrize('workspace_kind,profile_id,branch', [
    ('work', 'work', 'work.industry_knowledge'),
    ('study', 'mba', 'study.knowledge_understanding'),
    ('life', 'life', 'life.health'),
    ('work', 'general', 'work.industry_knowledge'),
    ('study', 'general', 'study.knowledge_understanding'),
    ('life', 'general', 'life.health'),
])
def test_profile_and_branch_must_match_host_trunk(workspace_kind, profile_id, branch):
    payload = mapping()
    payload.update(profile_id=profile_id, branch_keys=[branch])
    assert ontology_module().validate_mapping(payload, workspace_kind=workspace_kind)['status'] == 'pending_review'


@pytest.mark.parametrize('profile_id', ['mba', 'life', 'history', 'ecommerce'])
def test_incompatible_or_unknown_profile_workspaces_are_not_guessed(profile_id):
    payload = mapping()
    payload['profile_id'] = profile_id
    with pytest.raises(ValueError):
        ontology_module().validate_mapping(payload, workspace_kind='work')


def test_relation_subset_is_enforced_even_for_platform_registered_relations(monkeypatch):
    from app.memory import domain_profiles

    profiles = copy.deepcopy(domain_profiles.load_profiles())
    profiles['work']['relation_kinds'] = ['related']
    monkeypatch.setattr(domain_profiles, 'load_profiles', lambda: profiles)
    with pytest.raises(ValueError):
        ontology_module().validate_mapping(mapping(), workspace_kind='work')


def test_all_approved_target_fields_and_empty_optional_selections_are_allowed():
    payload = mapping()
    payload.update(relation_kinds=[], sample_refs=[], field_mappings={
        'title': 'name', 'type': 'kp_type', 'section': 'chapter',
        'summary': 'gist', 'body': 'detail', 'caution': 'trap', 'evidence': 'source_refs',
    })
    assert ontology_module().validate_mapping(payload, workspace_kind='work')['field_mappings'] == payload['field_mappings']


def test_schema_exposes_closed_fields_and_fixed_choices():
    schema = ontology_module().mapping_json_schema()
    assert schema['type'] == 'object'
    assert schema['additionalProperties'] is False
    assert set(schema['required']) == set(mapping()) == set(schema['properties'])
    properties = schema['properties']
    assert properties['ontology_version']['const'] == 'personal@1.0.0'
    assert properties['evidence_required']['type'] == 'boolean'
    assert properties['evidence_required']['const'] is True
    assert properties['branch_keys']['uniqueItems'] is True
    assert len(properties['branch_keys']['items']['enum']) == 23
    assert set(properties['field_mappings']['additionalProperties']['enum']) == {
        'name', 'kp_type', 'chapter', 'gist', 'detail', 'trap', 'source_refs',
    }
    assert json.loads(json.dumps(schema)) == schema


def test_registry_and_validation_do_not_open_database_or_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Ontology validation attempted database or network I/O')

    monkeypatch.setattr(sqlite3, 'connect', forbidden)
    monkeypatch.setattr(socket, 'socket', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    ontology = ontology_module()
    assert ontology.public_contract()['ontology_version'] == 'personal@1.0.0'
    ontology.mapping_json_schema()
    ontology.validate_mapping(mapping(), workspace_kind='work')
    payload = mapping()
    payload['status'] = 'approved'
    with pytest.raises(ValueError):
        ontology.validate_mapping(payload, workspace_kind='work')
