"""Frozen personal ontology and pure validation of model mapping proposals.

Branches organize objects; they grant neither workspace access nor approval.
Validation checks structure and profile compatibility, not evidence truth.
"""

from __future__ import annotations

from typing import Any

from . import domain_profiles
from .knowledge import NODE_TYPES, RELATION_KINDS

ONTOLOGY_VERSION = 'personal@1.0.0'

# Tuples keep the packaged registry independent of mutable files and callers.
_TRUNKS = (
    ('work', '\u5de5\u4f5c', 'WorkPal'),
    ('study', '\u5b66\u4e60', 'StudyPal'),
    ('life', '\u751f\u6d3b', 'LifePal'),
)
_BRANCHES = (
    ('work.industry_knowledge', '\u884c\u4e1a\u4e0e\u4e13\u4e1a\u77e5\u8bc6', 'work'),
    ('work.roles_responsibilities', '\u804c\u8d23\u4e0e\u89d2\u8272', 'work'),
    ('work.goals_outcomes', '\u76ee\u6807\u4e0e\u6210\u679c', 'work'),
    ('work.projects_execution', '\u9879\u76ee\u4e0e\u6267\u884c', 'work'),
    ('work.clients_collaboration', '\u5ba2\u6237\u4e0e\u534f\u4f5c', 'work'),
    ('work.resources_constraints', '\u8d44\u6e90\u4e0e\u7ea6\u675f', 'work'),
    ('work.decisions_experience', '\u51b3\u7b56\u4e0e\u7ecf\u9a8c', 'work'),
    ('study.goals_paths', '\u76ee\u6807\u4e0e\u8def\u5f84', 'study'),
    ('study.subjects_courses', '\u5b66\u79d1\u4e0e\u8bfe\u7a0b', 'study'),
    ('study.knowledge_understanding', '\u77e5\u8bc6\u4e0e\u7406\u89e3', 'study'),
    ('study.materials_evidence', '\u6750\u6599\u4e0e\u8bc1\u636e', 'study'),
    ('study.practice_application', '\u7ec3\u4e60\u4e0e\u5e94\u7528', 'study'),
    ('study.assessment_review', '\u8bc4\u4ef7\u4e0e\u590d\u4e60', 'study'),
    ('life.health', '\u8eab\u5fc3\u5065\u5eb7', 'life'),
    ('life.family_relationships', '\u5bb6\u5ead\u4e0e\u4eb2\u5bc6\u5173\u7cfb', 'life'),
    ('life.friends_social', '\u670b\u53cb\u4e0e\u793e\u4f1a\u8fde\u63a5', 'life'),
    ('life.finance_security', '\u8d22\u52a1\u4e0e\u4fdd\u969c', 'life'),
    ('life.home_daily', '\u5c45\u4f4f\u4e0e\u65e5\u5e38\u4e8b\u52a1', 'life'),
    ('life.learning_growth', '\u5b66\u4e60\u4e0e\u4e2a\u4eba\u6210\u957f', 'life'),
    ('life.interests_creation', '\u5174\u8da3\u3001\u5a31\u4e50\u4e0e\u521b\u4f5c', 'life'),
    ('life.travel_experiences', '\u65c5\u884c\u4e0e\u751f\u6d3b\u4f53\u9a8c', 'life'),
    ('life.news_society', '\u8d44\u8baf\u4e0e\u793e\u4f1a\u89c2\u5bdf', 'life'),
    ('life.direction_choices', '\u4eba\u751f\u65b9\u5411\u4e0e\u91cd\u8981\u9009\u62e9', 'life'),
)
_BUSINESS_GROUPS = (
    ('people_organizations', '\u4eba\u7269\u4e0e\u7ec4\u7ec7'),
    ('goals', '\u76ee\u6807'),
    ('matters_actions', '\u4e8b\u9879\u4e0e\u884c\u52a8'),
    ('knowledge_methods', '\u77e5\u8bc6\u4e0e\u65b9\u6cd5'),
    ('resources', '\u8d44\u6e90'),
    ('results_experience', '\u7ed3\u679c\u4e0e\u7ecf\u9a8c'),
)
_GOVERNANCE_RECORDS = (
    ('source', '\u6765\u6e90'), ('snapshot', '\u5feb\u7167'),
    ('evidence', '\u8bc1\u636e'), ('review', '\u5ba1\u6838'),
)
_CORE_RECORDS = (
    ('source', 'Source', 'Stable identity of an original material, independent of its location.'),
    ('snapshot', 'Snapshot', 'A version of a source; earlier versions retain their provenance.'),
    ('evidence', 'Evidence', 'Original source content with a locator and snapshot provenance.'),
    ('entity', 'Entity', 'An identifiable object, separate from its branch memberships.'),
    ('concept', 'Concept', 'A reusable concept, separate from a personal judgment or mastery state.'),
    ('statement', 'Statement', 'An evidence-backed relation claim with conditions, time, and review status.'),
    ('personal_state', 'PersonalState', 'A personal perspective or mastery state, separate from knowledge facts.'),
    ('review', 'Review', 'A human decision about a proposal, preserving its review history.'),
)
_TARGET_FIELDS = ('name', 'kp_type', 'chapter', 'gist', 'detail', 'trap', 'source_refs')
_NODE_TYPES = frozenset(NODE_TYPES)
_RELATION_KINDS = frozenset(RELATION_KINDS)
_MAPPING_FIELDS = frozenset({
    'ontology_version', 'profile_id', 'profile_version', 'branch_keys',
    'node_types', 'relation_kinds', 'field_mappings', 'evidence_required',
    'sample_refs', 'model_id', 'prompt_version',
})


def public_contract() -> dict[str, Any]:
    """Return an independent JSON contract, never personal data or permissions."""
    return {
        'ontology_version': ONTOLOGY_VERSION,
        'root': {'key': 'personal', 'display_name': '\u4e2a\u4eba', 'parent_key': None},
        'trunks': [
            {'key': key, 'display_name': name, 'parent_key': 'personal', 'workspace': workspace}
            for key, name, workspace in _TRUNKS
        ],
        'branches': [
            {'key': key, 'display_name': name, 'parent_key': parent}
            for key, name, parent in _BRANCHES
        ],
        'business_groups': [{'key': key, 'display_name': name} for key, name in _BUSINESS_GROUPS],
        'governance_records': [{'key': key, 'display_name': name} for key, name in _GOVERNANCE_RECORDS],
        'core_records': [
            {'key': key, 'display_name': name, 'description': description}
            for key, name, description in _CORE_RECORDS
        ],
        'mapping_policy': {
            'branch_membership_grants_access': False,
            'validation_grants_approval': False,
            'branch_membership_is_node_type': False,
            'core_records_are_node_types': False,
            'cross_workspace_sharing_requires_authorization': True,
            'multiple_branches_within_trunk': True,
            'type_extensions_allowed': False,
            'evidence_required': True,
            'output_status': 'pending_review',
            'allowed_target_fields': list(_TARGET_FIELDS),
            'node_types': sorted(_NODE_TYPES),
            'relation_kinds': sorted(_RELATION_KINDS),
            'scope': 'Branches organize objects; workspace authorization is enforced separately by the host.',
            'validation_scope': 'Structure and profile compatibility only; no evidence verification or publication.',
        },
    }


def _string(value: Any, field: str, *, limit: int = 256) -> str:
    if type(value) is not str or not value.strip() or len(value) > limit:
        raise ValueError(f'{field} must be a bounded nonblank string')
    return value.strip()


def _strings(value: Any, field: str, *, minimum: int = 0, maximum: int = 64) -> list[str]:
    if type(value) is not list or not minimum <= len(value) <= maximum:
        raise ValueError(f'{field} must be a bounded JSON array')
    result = [_string(item, field, limit=512) for item in value]
    if len(result) != len(set(result)):
        raise ValueError(f'{field} contains duplicate values')
    return result


def validate_mapping(payload: dict, *, workspace_kind: str) -> dict[str, Any]:
    """Validate a proposal without approving, persisting, or resolving evidence.

    ``workspace_kind`` must come from the host's authorized workspace record,
    never a model field. Invalid proposals raise ValueError without echoing data.
    """
    if type(workspace_kind) is not str or workspace_kind not in ('work', 'study', 'life'):
        raise ValueError('workspace_kind must be work, study, or life')
    if type(payload) is not dict or payload.keys() != _MAPPING_FIELDS:
        raise ValueError('mapping must contain exactly the agreed fields')
    result = {key: _string(payload[key], key) for key in (
        'ontology_version', 'profile_id', 'profile_version', 'model_id', 'prompt_version',
    )}
    if result['ontology_version'] != ONTOLOGY_VERSION:
        raise ValueError('unknown ontology_version')
    profile = domain_profiles.load_profiles().get(result['profile_id'])
    if profile is None:
        raise ValueError('unknown profile_id')
    if result['profile_version'] != profile['version']:
        raise ValueError('profile_version does not match the registered profile')
    expected_workspace = next(workspace for key, _, workspace in _TRUNKS if key == workspace_kind)
    profile_workspace = profile['workspace'].strip().casefold()
    if profile_workspace != expected_workspace.casefold() and not (
        result['profile_id'] == 'general' and profile['workspace'] == ''
    ):
        raise ValueError('profile is incompatible with workspace_kind')
    branches = _strings(payload['branch_keys'], 'branch_keys', minimum=1, maximum=23)
    allowed_branches = {key for key, _, parent in _BRANCHES if parent == workspace_kind}
    if not set(branches) <= allowed_branches:
        raise ValueError('branch_keys must be registered branches of the host trunk')
    node_types = _strings(payload['node_types'], 'node_types', minimum=1, maximum=len(_NODE_TYPES))
    relations = _strings(payload['relation_kinds'], 'relation_kinds', maximum=len(_RELATION_KINDS))
    if not set(node_types) <= _NODE_TYPES.intersection(profile['node_types']):
        raise ValueError('node_types must be a subset of the registered profile')
    if not set(relations) <= _RELATION_KINDS.intersection(profile['relation_kinds']):
        raise ValueError('relation_kinds must be a subset of the registered profile')
    fields = payload['field_mappings']
    if type(fields) is not dict or len(fields) > len(_TARGET_FIELDS):
        raise ValueError('field_mappings must be a bounded JSON object')
    normalized_fields: dict[str, str] = {}
    for source, target in fields.items():
        source = _string(source, 'field_mappings source')
        target = _string(target, 'field_mappings target')
        if source in normalized_fields or target in normalized_fields.values():
            raise ValueError('field_mappings contains duplicate source or target fields')
        if target not in _TARGET_FIELDS:
            raise ValueError('field_mappings target is not an approved field')
        normalized_fields[source] = target
    if payload['evidence_required'] is not True:
        raise ValueError('evidence_required must be the boolean true')
    result.update(
        branch_keys=branches, node_types=node_types, relation_kinds=relations,
        field_mappings=normalized_fields, evidence_required=True,
        sample_refs=_strings(payload['sample_refs'], 'sample_refs'),
        status='pending_review',
    )
    return result


def mapping_json_schema() -> dict[str, Any]:
    """Model-facing proposal schema; cross-field and host checks remain required."""
    def string_schema(limit: int = 256) -> dict[str, Any]:
        return {'type': 'string', 'minLength': 1, 'maxLength': limit, 'pattern': r'\S'}

    def array_schema(choices: list[str] | None, minimum: int, maximum: int) -> dict[str, Any]:
        item = string_schema(512)
        if choices is not None:
            item['enum'] = choices
        return {
            'type': 'array', 'items': item, 'uniqueItems': True,
            'minItems': minimum, 'maxItems': maximum,
        }

    return {
        '$schema': 'https://json-schema.org/draft/2020-12/schema',
        'title': 'Personal ontology mapping proposal',
        'description': (
            'Select existing branches within the host workspace trunk and types/relations within the '
            'exact registered profile version. The host validates these cross-field constraints. '
            'Branches grant no access. Validation never approves or publishes a proposal.'
        ),
        'type': 'object', 'additionalProperties': False,
        'required': sorted(_MAPPING_FIELDS),
        'properties': {
            'ontology_version': {'type': 'string', 'const': ONTOLOGY_VERSION},
            'profile_id': string_schema(), 'profile_version': string_schema(),
            'branch_keys': array_schema([key for key, _, _ in _BRANCHES], 1, 23),
            'node_types': array_schema(sorted(_NODE_TYPES), 1, len(_NODE_TYPES)),
            'relation_kinds': array_schema(sorted(_RELATION_KINDS), 0, len(_RELATION_KINDS)),
            'field_mappings': {
                'type': 'object', 'maxProperties': len(_TARGET_FIELDS),
                'propertyNames': string_schema(),
                'additionalProperties': {'type': 'string', 'enum': list(_TARGET_FIELDS)},
            },
            'evidence_required': {'type': 'boolean', 'const': True},
            'sample_refs': array_schema(None, 0, 64),
            'model_id': string_schema(), 'prompt_version': string_schema(),
        },
    }
