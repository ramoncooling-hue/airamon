import json
from unittest.mock import patch

import pytest

from app.hub import config_composer


def _model_reply(payload):
    return lambda *args, **kwargs: json.dumps(payload, ensure_ascii=False)


def test_preview_creates_a_safe_skill_draft_without_writing_files(monkeypatch):
    payload = {
        'target': 'skill',
        'draft': {
            'key': 'paper-research',
            'name': '论文检索',
            'version': '1.0.0',
            'description': '检索公开资料并整理来源',
            'content': '# 论文检索\n\n先明确问题，再检索并标注来源。',
        },
    }
    monkeypatch.setattr(config_composer.llm, 'chat', _model_reply(payload))
    with patch.object(config_composer.registry, 'create_skill') as create:
        result = config_composer.preview('帮我创建一个论文检索 Skill', target='skill')

    create.assert_not_called()
    assert result['target'] == 'skill'
    assert result['draft']['key'] == 'paper-research'
    assert result['requires_confirmation'] is True
    assert result['missing'] == []


def test_preview_rejects_secret_values_and_unknown_mcp_fields(monkeypatch):
    payload = {
        'target': 'mcp',
        'draft': {
            'id': 'tavily_search',
            'label': 'Tavily 搜索',
            'transport': 'streamable_http',
            'url': 'https://example.com/mcp',
            'workspace': 'studypal',
            'network': True,
            'tools': {'search': {'read_only': True}},
            'api_key': 'should-never-be-accepted',
        },
    }
    monkeypatch.setattr(config_composer.llm, 'chat', _model_reply(payload))
    with pytest.raises(ValueError, match='不支持|密钥|凭据'):
        config_composer.preview('接入一个搜索 MCP', target='mcp')


def test_confirm_skill_calls_existing_registry_only_after_preview(monkeypatch):
    draft = {
        'target': 'skill',
        'draft': {
            'key': 'local-reader',
            'name': '本地资料读取',
            'version': '1.0.0',
            'description': '读取用户选定的本地资料',
            'content': '# 本地资料读取',
        },
    }
    preview = config_composer.validate_preview(draft)
    with patch.object(config_composer.registry, 'create_skill', return_value={'key': 'local-reader'}) as create:
        result = config_composer.confirm(preview)

    create.assert_called_once_with(preview['draft'])
    assert result['created']['key'] == 'local-reader'


def test_mcp_preview_defaults_to_disabled_untrusted_and_env_name_only(monkeypatch):
    payload = {
        'target': 'mcp',
        'draft': {
            'id': 'tavily_search',
            'label': 'Tavily 搜索',
            'transport': 'streamable_http',
            'url': 'https://example.com/mcp',
            'workspace': 'studypal',
            'network': True,
            'tools': {'search': {'read_only': True}},
            'env_names': ['TAVILY_API_KEY'],
        },
    }
    monkeypatch.setattr(config_composer.llm, 'chat', _model_reply(payload))
    result = config_composer.preview('接入 Tavily 搜索 MCP', target='mcp')
    assert result['draft']['enabled'] is False
    assert result['draft']['trusted'] is False
    assert result['draft']['env_names'] == ['TAVILY_API_KEY']
    assert 'TAVILY_API_KEY' in json.dumps(result, ensure_ascii=False)
    assert '"api_key"' not in json.dumps(result, ensure_ascii=False).lower()
