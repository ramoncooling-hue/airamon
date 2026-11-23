"""T2.7/O-TEST-2: agent_runtime/security.py 的凭据拒绝边界。

security.py 只有一个公开函数 reject_secrets()：按字段名拒绝凭据，值本身不做
扫描（值的脱敏在日志/导出层）。路径与命令白名单不在该模块，而在
maintenance/files.py、integrations/mcp_bridge.py 等调用方；本文件只冻结该模块
现有契约，不改产品代码。
"""

import pytest

from app.agent_runtime.security import SECRET_FIELD_RE, reject_secrets


CREDENTIAL_KEYS = [
    'api_key', 'apiKey', 'API_KEY', 'x-api-key', 'password', 'passwd',
    'cookie', 'Cookie', 'authorization', 'Authorization', 'credential',
    'secret', 'client_secret', 'token', 'access_token', 'refresh_token',
]


@pytest.mark.parametrize('key', CREDENTIAL_KEYS)
def test_credential_field_names_are_rejected(key):
    with pytest.raises(ValueError, match='不允许保存凭据'):
        reject_secrets({key: 'value'})


def test_rejection_reports_the_offending_path():
    with pytest.raises(ValueError) as error:
        reject_secrets({'memory': {'profile': {'token': 'x'}}})
    assert 'value.memory.profile.token' in str(error.value)


def test_list_indexes_appear_in_the_path():
    with pytest.raises(ValueError) as error:
        reject_secrets({'items': [{'name': 'ok'}, {'password': 'x'}]})
    assert 'value.items[1].password' in str(error.value)


def test_deep_mixed_nesting_is_walked_completely():
    with pytest.raises(ValueError) as error:
        reject_secrets({'a': [{'b': [{'c': {'client_secret': 'x'}}]}]})
    assert 'value.a[0].b[0].c.client_secret' in str(error.value)


def test_benign_structures_are_allowed():
    value = {
        'key': 'study-skill', 'name': '复习', 'version': '1.0.0',
        'content': '# 正文', 'enabled': True, 'read_only': True,
        'description': '普通描述', 'workspace': 'studypal',
        'items': [{'kp_type': 'concept', 'confidence': 0.8}],
        'args': ['--flag', 'value'],
    }
    assert reject_secrets(value) is None


def test_only_field_names_are_scanned_not_values():
    # 值里的密钥形态字符串不归本模块管，交给日志/导出脱敏层。
    assert reject_secrets({'note': 'sk-live-abcdef',
                           'url': 'https://example.com/?key=abcdef'}) is None


def test_conservative_substring_matching_is_documented():
    # 正则是 search：字段名里包含 token 的复合词也会被保守拒绝。
    assert SECRET_FIELD_RE.search('trigram_tokenizer')
    with pytest.raises(ValueError, match='不允许保存凭据'):
        reject_secrets({'tokenizer': 'trigram'})


@pytest.mark.parametrize('value', [None, '', 'text', 3, 3.5, True, [], {}])
def test_scalars_and_empty_containers_pass(value):
    assert reject_secrets(value) is None


def test_error_message_never_echoes_the_secret_value():
    with pytest.raises(ValueError) as error:
        reject_secrets({'api_key': 'sk-live-should-not-appear'})
    message = str(error.value)
    assert 'sk-live-should-not-appear' not in message
    assert 'api_key' in message


def test_top_level_path_defaults_to_value():
    with pytest.raises(ValueError) as error:
        reject_secrets({'secret': 'x'})
    assert str(error.value).startswith('value')


def test_custom_path_prefix_is_used_in_messages():
    with pytest.raises(ValueError) as error:
        reject_secrets({'token': 'x'}, 'proposal')
    assert 'proposal.token' in str(error.value)
