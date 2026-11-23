"""Domain routing contracts, independent of personal data and workspace ownership."""

import copy
import json

import pytest

from app.memory import domain_profiles, wiki


@pytest.fixture(autouse=True)
def isolated_profile_cache():
    domain_profiles.clear_cache()
    yield
    domain_profiles.clear_cache()


@pytest.fixture
def work_profile():
    return json.loads((domain_profiles.PROFILE_DIR / 'work.json').read_text(encoding='utf-8'))


def write_profile(directory, filename, profile):
    (directory / filename).write_text(json.dumps(profile), encoding='utf-8')


@pytest.mark.parametrize('course,expected', [
    ('\u4f1a\u8ba1\u5b66', 'mba'),
    ('\u7ec4\u7ec7\u5de5\u4f5c\u65b9\u6cd5', 'mba'),
    ('AI\u5de5\u4f5c\u7cfb\u7edf', 'work'),
    ('World HISTORY', 'history'),
    ('Ecommerce operations', 'ecommerce'),
    ('Unclassified course', 'mba'),
    ('', 'mba'),
])
def test_course_only_routing_keeps_legacy_behavior(course, expected):
    assert domain_profiles.profile_for_course(course)['id'] == expected


@pytest.mark.parametrize('workspace,expected', [
    ('WorkPal', 'work'), ('workpal', 'work'), ('  WORKPAL  ', 'work'),
    ('StudyPal', 'mba'), ('studypal', 'mba'),
    ('HistoryPal', 'history'), ('historypal', 'history'),
    ('EcommercePal', 'ecommerce'), ('ecommercepal', 'ecommerce'),
    ('LifePal', 'life'), ('lifepal', 'life'), ('  LIFEPAL  ', 'life'),
    ('Custom MBA', 'general'), ('WorkPal archive', 'general'),
    ('enterprise-ai', 'general'),
])
def test_explicit_workspace_wins_over_conflicting_titles(workspace, expected):
    for course in ('', 'Ecommerce History MBA Work', '\u7ec4\u7ec7\u7ba1\u7406'):
        assert domain_profiles.profile_for_course(course, workspace)['id'] == expected


def test_blank_workspace_keeps_course_only_compatibility():
    assert domain_profiles.profile_for_course('AI\u5de5\u4f5c\u7cfb\u7edf', '  ')['id'] == 'work'


@pytest.mark.parametrize('profile_id', ['mba', 'work', 'history', 'ecommerce', 'life', 'general'])
def test_legacy_domain_id_argument_is_resolved_before_title_patterns(profile_id):
    assert domain_profiles.profile_for_course('Ecommerce History MBA Work', profile_id)['id'] == profile_id


@pytest.mark.parametrize('workspace,expected', [('LifePal', 'life'), ('PrivateSpace', 'general')])
def test_personal_fallback_is_neutral_and_public_contract_is_preserved(workspace, expected):
    profile = domain_profiles.public_profile('MBA exams', workspace)
    assert profile['id'] == expected
    assert 'exam_point' not in profile['node_types']
    assert 'source_refs' in profile['required_fields']
    assert profile['quality_rules']
    assert profile['workspace'] == ('LifePal' if expected == 'life' else '')
    assert set(profile) == {
        'id', 'version', 'workspace', 'display_name', 'node_types', 'relation_kinds',
        'required_fields', 'quality_rules', 'extraction_sections', 'wiki_rules',
    }


@pytest.mark.parametrize('selector,expected', [
    ('WorkPal', 'work'), ('LifePal', 'life'), ('PrivateSpace', 'general'),
    ('work', 'work'), ('history', 'history'), ('ecommerce', 'ecommerce'),
])
def test_wiki_dictionary_and_note_keep_the_same_profile_on_round_trip(tmp_path, selector, expected):
    result = wiki.write_artifacts(
        course_dir=tmp_path, course='MBA plans', source_path='source.md',
        source_sha256='test-hash', content='Source-backed note.', headings=[],
        evidence=[], domain=selector,
    )
    dictionary = json.loads((tmp_path / 'dictionary' / 'source.json').read_text(encoding='utf-8'))
    note = (tmp_path / 'notes' / 'source.md').read_text(encoding='utf-8')
    assert dictionary['domain'] == expected
    assert f'domain: {expected}\n' in note
    assert f'workspace: {json.dumps(dictionary["workspace"])}\n' in note
    assert result['dictionary'] == str(tmp_path / 'dictionary' / 'source.json')


def test_enterprise_ai_remains_a_domain_inside_workpal(tmp_path, monkeypatch, work_profile):
    enterprise = copy.deepcopy(work_profile)
    enterprise.update(id='enterprise-ai', course_patterns=['Enterprise AI'])
    enterprise.pop('workspace_default', None)
    work_profile['workspace_default'] = True
    write_profile(tmp_path, 'a-enterprise.json', enterprise)
    write_profile(tmp_path, 'z-work.json', work_profile)
    monkeypatch.setattr(domain_profiles, 'PROFILE_DIR', tmp_path)

    assert domain_profiles.profile_for_course('Enterprise AI strategy', 'workpal')['id'] == 'enterprise-ai'
    assert domain_profiles.profile_for_course('MBA operations', 'WorkPal')['id'] == 'work'
    selected = domain_profiles.profile_for_course('MBA operations', 'enterprise-ai')
    assert (selected['id'], selected['workspace']) == ('enterprise-ai', 'WorkPal')


@pytest.mark.parametrize('value', [None, [], 'profile', 3])
def test_profile_root_must_be_an_object(tmp_path, monkeypatch, value):
    write_profile(tmp_path, 'bad.json', value)
    monkeypatch.setattr(domain_profiles, 'PROFILE_DIR', tmp_path)
    with pytest.raises(ValueError, match='bad.json'):
        domain_profiles.load_profiles()


@pytest.mark.parametrize('field,value', [
    ('id', ['work']), ('id', 'Work'), ('id', ''),
    ('version', 1), ('version', True), ('version', ''), ('version', '1.0'),
    ('version', '01.0.0'), ('version', '1.0.0-01'),
    ('workspace', None), ('workspace', []), ('workspace', '  '),
    ('display_name', 7), ('display_name', '  '),
    ('node_types', ['concept', 'concept']), ('node_types', [{}]),
    ('node_types', ['unregistered']), ('node_types', []),
    ('relation_kinds', ['related', 'related']), ('relation_kinds', [[]]),
    ('relation_kinds', ['unregistered']),
    ('required_fields', ['name']), ('required_fields', ['source_refs', 1]),
    ('required_fields', ['source_refs', 'source_refs']),
    ('quality_rules', [None]), ('quality_rules', ['']), ('quality_rules', ['  ']),
    ('quality_rules', ['rule', 'rule']),
    ('course_patterns', 'Work'), ('course_patterns', [1]),
    ('course_patterns', ['']), ('course_patterns', ['Work', 'work']),
    ('extraction_sections', 'Summary'), ('extraction_sections', ['']),
    ('wiki_rules', []), ('wiki_rules', {}),
    ('wiki_rules', {'frontmatter_schema': 1, 'link_style': 'wikilink', 'evidence_locator': 'line'}),
    ('workspace_default', 'true'), ('workspace_default', 1),
])
def test_invalid_profile_fields_fail_with_path(tmp_path, monkeypatch, work_profile, field, value):
    work_profile[field] = value
    write_profile(tmp_path, 'bad.json', work_profile)
    monkeypatch.setattr(domain_profiles, 'PROFILE_DIR', tmp_path)
    with pytest.raises(ValueError, match='bad.json'):
        domain_profiles.load_profiles()


def test_missing_required_field_reports_path(tmp_path, monkeypatch, work_profile):
    del work_profile['version']
    write_profile(tmp_path, 'missing.json', work_profile)
    monkeypatch.setattr(domain_profiles, 'PROFILE_DIR', tmp_path)
    with pytest.raises(ValueError, match='missing.json.*version'):
        domain_profiles.load_profiles()


def test_duplicate_ids_are_rejected_instead_of_silently_overwritten(tmp_path, monkeypatch, work_profile):
    write_profile(tmp_path, 'first.json', work_profile)
    write_profile(tmp_path, 'second.json', work_profile)
    monkeypatch.setattr(domain_profiles, 'PROFILE_DIR', tmp_path)
    with pytest.raises(ValueError, match='second.json.*first.json'):
        domain_profiles.load_profiles()


@pytest.mark.parametrize('two_defaults', [False, True])
def test_ambiguous_workspace_defaults_are_rejected(tmp_path, monkeypatch, work_profile, two_defaults):
    other = copy.deepcopy(work_profile)
    other.update(id='enterprise-ai', workspace='workpal', workspace_default=two_defaults)
    work_profile['workspace_default'] = two_defaults
    write_profile(tmp_path, 'first.json', work_profile)
    write_profile(tmp_path, 'second.json', other)
    monkeypatch.setattr(domain_profiles, 'PROFILE_DIR', tmp_path)
    with pytest.raises(ValueError, match='second.json.*first.json'):
        domain_profiles.load_profiles()


@pytest.mark.parametrize('raw', [
    '{"id":"work","id":"other"}',
    '{"wiki_rules":{"link_style":"wikilink","link_style":"other"}}',
    '{invalid json',
])
def test_bad_json_reports_the_file_including_duplicate_keys(tmp_path, monkeypatch, raw):
    (tmp_path / 'bad.json').write_text(raw, encoding='utf-8')
    monkeypatch.setattr(domain_profiles, 'PROFILE_DIR', tmp_path)
    with pytest.raises(ValueError, match='bad.json'):
        domain_profiles.load_profiles()


@pytest.mark.parametrize('version', ['0.0.0', '12.3.4', '1.2.3-rc.1+build.7'])
def test_valid_extensions_and_optional_empty_lists_are_preserved(tmp_path, monkeypatch, work_profile, version):
    work_profile.update(version=version, course_patterns=[], extraction_sections=[], extension={'enabled': True})
    write_profile(tmp_path, 'work.json', work_profile)
    monkeypatch.setattr(domain_profiles, 'PROFILE_DIR', tmp_path)
    assert domain_profiles.load_profiles()['work'] == work_profile


def test_empty_profile_directory_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(domain_profiles, 'PROFILE_DIR', tmp_path)
    with pytest.raises(ValueError):
        domain_profiles.load_profiles()
