"""T2.7/O-TEST-2: hub/config_composer.py 草稿确认/拒绝与脱敏边界。

config_composer 只生成待确认草稿：validation 在 Hub/MCP 服务里，模型输出永远
不能直接写文件、起进程或保存凭据。本文件覆盖非法输入、版本冲突、确认/拒绝与
返回 payload 的脱敏边界。
"""

import json

import pytest

from app.hub import config_composer


def _skill_payload(**draft_overrides):
    draft = {
        'key': 'paper-research',
        'name': '论文检索',
        'version': '1.0.0',
        'description': '检索公开资料并整理来源',
        'content': '# 论文检索\n\n先明确问题，再检索并标注来源。',
    }
    draft.update(draft_overrides)
    return {'target': 'skill', 'draft': draft}


def _mcp_payload(**draft_overrides):
    draft = {
        'id': 'tavily_search',
        'label': 'Tavily 搜索',
        'transport': 'streamable_http',
        'url': 'https://example.com/mcp',
        'workspace': 'studypal',
        'network': True,
        'tools': {'search': {'read_only': True}},
        'env_names': ['TAVILY_API_KEY'],
    }
    draft.update(draft_overrides)
    return {'target': 'mcp', 'draft': draft, 'base_revision': 'rev-1'}


# ── 结构与白名单 ────────────────────────────────────────────────────────────


def test_validate_preview_rejects_unknown_target():
    with pytest.raises(ValueError, match='配置目标只能是 skill 或 mcp'):
        config_composer.validate_preview({'target': 'agent', 'draft': {}})


def test_validate_preview_requires_object_payload():
    with pytest.raises(ValueError, match='必须是对象'):
        config_composer.validate_preview('not-a-dict')


def test_skill_draft_rejects_unknown_fields():
    with pytest.raises(ValueError, match='不支持字段'):
        config_composer.validate_preview(_skill_payload(hooks=[]))


@pytest.mark.parametrize('key', ['中文标识', '-lead', 'a b', 'x' * 65])
def test_skill_draft_rejects_invalid_keys(key):
    with pytest.raises(ValueError, match='只能包含字母、数字、下划线和短横线'):
        config_composer.validate_preview(_skill_payload(key=key))


def test_skill_draft_requires_name_and_content():
    with pytest.raises(ValueError, match='Skill 名称 不能为空'):
        config_composer.validate_preview(_skill_payload(name='   '))
    payload = _skill_payload()
    payload['draft'].pop('content')
    with pytest.raises(ValueError, match='Skill 内容 不能为空'):
        config_composer.validate_preview(payload)


@pytest.mark.parametrize('content', [
    'bash rm -rf /tmp/x',
    'python -c "print(1)"',
    'node server.js',
    '# 标题\n\nsh -c id',
])
def test_skill_draft_rejects_inline_shell(content):
    with pytest.raises(ValueError, match='内联指令'):
        config_composer.validate_preview(_skill_payload(content=content))


def test_skill_draft_defaults_and_normalization():
    payload = _skill_payload(key='  paper-research  ', description='  检索并标注来源  ',
                             enabled=False)
    payload['draft'].pop('version')
    draft = config_composer.validate_preview(payload)['draft']
    assert draft['key'] == 'paper-research'
    assert draft['version'] == '1.0.0'
    assert draft['description'] == '检索并标注来源'
    assert draft['enabled'] is True  # 自然语言草稿总是先启用为可编辑 Skill


@pytest.mark.parametrize('payload', [
    _skill_payload(secret='x'),
    _skill_payload(content='token=abcdef'),
    _skill_payload(description='password: hunter2'),
])
def test_secret_fields_and_assignments_are_rejected(payload):
    with pytest.raises(ValueError, match='密钥或凭据值'):
        config_composer.validate_preview(payload)


def test_ordinary_prose_mentioning_a_token_is_allowed():
    result = config_composer.validate_preview(
        _skill_payload(description='不保存 token，只声明变量名'))
    assert result['draft']['description'] == '不保存 token，只声明变量名'


# ── MCP 草稿边界 ───────────────────────────────────────────────────────────


@pytest.mark.parametrize('tools', [
    {'search': {'read_only': False}},
    {'search': {'read_only': 'yes'}},
    {'search': {}},
    {'search': {'read_only': True, 'write': True}},
    {'search': 'read_only'},
])
def test_mcp_tools_must_be_read_only(tools):
    with pytest.raises(ValueError, match='只读|必须是对象'):
        config_composer.validate_preview(_mcp_payload(tools=tools))


@pytest.mark.parametrize('env_names', [
    ['abc'],                    # 小写不是变量名
    ['API_KEY=value'],          # 变量值不是变量名
    'API_KEY',                  # 必须是数组
    [1],
])
def test_mcp_env_names_only_accept_names(env_names):
    with pytest.raises(ValueError, match='环境变量|密钥|凭据'):
        config_composer.validate_preview(_mcp_payload(env_names=env_names))


def test_mcp_transport_and_url_validation():
    with pytest.raises(ValueError, match='传输方式'):
        config_composer.validate_preview(_mcp_payload(transport='sse'))
    with pytest.raises(ValueError, match='http 或 https'):
        config_composer.validate_preview(_mcp_payload(url='ftp://example.com/mcp'))
    with pytest.raises(ValueError, match='MCP 服务 URL 不能为空'):
        config_composer.validate_preview(_mcp_payload(url='  '))


def test_mcp_stdio_command_and_args_validation():
    payload = _mcp_payload(transport='stdio')
    payload['draft'].pop('url')
    with pytest.raises(ValueError, match='启动命令 不能为空'):
        config_composer.validate_preview(payload)
    payload['draft']['command'] = 'uvx'
    payload['draft']['args'] = 'not-a-list'
    with pytest.raises(ValueError, match='字符串数组'):
        config_composer.validate_preview(payload)
    payload['draft']['args'] = ['ok'] * 40
    draft = config_composer.validate_preview(payload)['draft']
    assert len(draft['args']) == 32


def test_mcp_course_must_be_string():
    with pytest.raises(ValueError, match='课程必须是字符串'):
        config_composer.validate_preview(_mcp_payload(course=5))


def test_mcp_draft_is_disabled_untrusted_and_name_only():
    result = config_composer.validate_preview(_mcp_payload())
    draft = result['draft']
    assert draft['enabled'] is False
    assert draft['trusted'] is False
    assert draft['env_names'] == ['TAVILY_API_KEY']
    assert draft['bearer_env'] == 'TAVILY_API_KEY'
    assert result['base_revision'] == 'rev-1'
    assert result['requires_confirmation'] is True


# ── preview：模型输出只是草稿 ──────────────────────────────────────────────


def test_preview_requires_prompt_and_valid_target():
    with pytest.raises(ValueError, match='请先描述'):
        config_composer.preview('   ', target='skill')
    with pytest.raises(ValueError, match='配置目标只能是 skill 或 mcp'):
        config_composer.preview('创建一个技能', target='agent')


def test_preview_rejects_invalid_json(monkeypatch):
    monkeypatch.setattr(config_composer.llm, 'chat', lambda *a, **k: 'not json')
    with pytest.raises(ValueError, match='没有返回有效的配置 JSON'):
        config_composer.preview('创建一个技能', target='skill')


def test_preview_rejects_target_mismatch(monkeypatch):
    monkeypatch.setattr(config_composer.llm, 'chat',
                        lambda *a, **k: json.dumps(_mcp_payload()))
    with pytest.raises(ValueError, match='与当前页面不一致'):
        config_composer.preview('创建一个技能', target='skill')


def test_preview_rejects_model_draft_with_secret(monkeypatch):
    payload = _skill_payload(api_key='should-not-pass')
    monkeypatch.setattr(config_composer.llm, 'chat',
                        lambda *a, **k: json.dumps(payload, ensure_ascii=False))
    with pytest.raises(ValueError, match='密钥或凭据值'):
        config_composer.preview('创建一个技能', target='skill')


def test_preview_is_only_a_warning_draft(monkeypatch):
    payload = _skill_payload()
    calls = []
    monkeypatch.setattr(config_composer.llm, 'chat',
                        lambda *a, **k: json.dumps(payload, ensure_ascii=False))
    monkeypatch.setattr(config_composer.registry, 'create_skill',
                        lambda draft: calls.append(draft) or {'key': draft['key']})
    result = config_composer.preview('帮我创建一个论文检索 Skill', target='skill')
    assert calls == []
    assert result['requires_confirmation'] is True
    assert result['warnings'] and '预览' in result['warnings'][0]


# ── confirm / 版本冲突 / 脱敏 ──────────────────────────────────────────────


def test_confirm_skill_calls_registry_once(monkeypatch):
    payload = _skill_payload()
    validated = config_composer.validate_preview(payload)
    calls = []

    def fake_create(draft):
        calls.append(draft)
        return {'key': draft['key']}

    monkeypatch.setattr(config_composer.registry, 'create_skill', fake_create)
    result = config_composer.confirm(validated)
    assert len(calls) == 1
    assert calls[0] == validated['draft']
    assert result['ok'] is True
    assert result['target'] == 'skill'
    assert '绑定' in result['next']


def test_confirm_rejects_invalid_draft_without_writing(monkeypatch):
    calls = []
    monkeypatch.setattr(config_composer.registry, 'create_skill',
                        lambda draft: calls.append(draft) or {'key': 'x'})
    with pytest.raises(ValueError, match='密钥或凭据值'):
        config_composer.confirm(_skill_payload(content='token=abcdef'))
    assert calls == []


def test_confirm_mcp_rejects_stale_revision(monkeypatch):
    payload = _mcp_payload()
    payload['base_revision'] = 'old-rev'  # base_revision 是顶层字段，不是 draft 字段
    validated = config_composer.validate_preview(payload)
    monkeypatch.setattr(config_composer.mcp_settings, 'read',
                        lambda: {'servers': [], 'revision': 'new-rev'})
    saved = []
    monkeypatch.setattr(config_composer.mcp_settings, 'save',
                        lambda servers, revision: saved.append((servers, revision)))
    with pytest.raises(ValueError, match='配置已变化'):
        config_composer.confirm(validated)
    assert saved == []


def test_confirm_mcp_saves_validated_draft_with_current_revision(monkeypatch):
    monkeypatch.setattr(config_composer.mcp_settings, 'read',
                        lambda: {'servers': [{'id': 'other'}], 'revision': 'rev-1'})
    validated = config_composer.validate_preview(_mcp_payload())
    saved = {}

    def fake_save(servers, revision):
        saved['servers'] = servers
        saved['revision'] = revision
        return {'id': 'tavily_search'}

    monkeypatch.setattr(config_composer.mcp_settings, 'save', fake_save)
    result = config_composer.confirm(validated)
    assert saved['revision'] == 'rev-1'
    assert [server['id'] for server in saved['servers']] == ['other', 'tavily_search']
    assert result['ok'] is True
    assert 'MCP' in result['next']
    text = json.dumps(result, ensure_ascii=False)
    assert '"api_key"' not in text.lower()


def test_preview_payload_carries_env_names_not_credentials():
    validated = config_composer.validate_preview(_mcp_payload())
    text = json.dumps(validated, ensure_ascii=False)
    assert 'TAVILY_API_KEY' in text          # 变量名可以出现
    assert 'TAVILY_API_KEY=' not in text     # 变量值不允许出现
    assert '"api_key"' not in text.lower()
    assert '"secret"' not in text.lower()
