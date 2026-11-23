"""O-OBS-1: a locateable, redacted, rotating local log file."""

from __future__ import annotations

import json
import logging
import stat

import pytest

from app import logging_setup


@pytest.fixture(autouse=True)
def _clean_mindock_handlers():
    yield
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, '_mindock_owned', False):
            root.removeHandler(handler)
            handler.close()


def _flush():
    for handler in logging.getLogger().handlers:
        handler.flush()


def _log_warning(message, *args):
    logging.getLogger('mindock.test').warning(message, *args)
    _flush()


def _write_settings(data_dir, payload):
    target = data_dir / 'app' / 'settings.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding='utf-8')
    return target


def test_setup_creates_private_log_file_and_records_lines(tmp_path):
    path = logging_setup.setup_logging(tmp_path)
    assert path == tmp_path / 'app' / 'logs' / 'mindock.log'
    _log_warning('hello 世界')
    text = path.read_text(encoding='utf-8')
    assert 'hello 世界' in text
    assert 'WARNING' in text
    assert 'mindock.test' in text
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_setup_is_idempotent_and_does_not_duplicate_lines(tmp_path):
    first = logging_setup.setup_logging(tmp_path)
    second = logging_setup.setup_logging(tmp_path)
    assert first == second
    owned = [h for h in logging.getLogger().handlers
             if getattr(h, '_mindock_owned', False)]
    assert len(owned) == 1
    _log_warning('one-and-only-line')
    assert first.read_text(encoding='utf-8').count('one-and-only-line') == 1


def test_session_token_and_model_keys_never_reach_disk(tmp_path, monkeypatch):
    monkeypatch.setenv('MINDOCK_SESSION_TOKEN', 'session-secret-value-123')
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-openai-secret-123456')
    path = logging_setup.setup_logging(tmp_path)
    _log_warning('token=%s key %s', 'session-secret-value-123', 'sk-openai-secret-123456')
    _log_warning('Authorization: Bearer abcdefghijklmnop')
    _log_warning('api_key=sk-live-abcdefgh')
    text = path.read_text(encoding='utf-8')
    for secret in ('session-secret-value-123', 'sk-openai-secret-123456',
                   'abcdefghijklmnop', 'sk-live-abcdefgh'):
        assert secret not in text
    assert logging_setup.REDACTED in text


def test_default_log_path_follows_the_data_directory(tmp_path, monkeypatch):
    monkeypatch.setenv('CYBERPAL_DATA_DIR', str(tmp_path))
    path = logging_setup.setup_logging()
    assert path == tmp_path / 'app' / 'logs' / 'mindock.log'
    assert path.exists()


def test_unwritable_data_dir_is_best_effort(tmp_path):
    blocked = tmp_path / 'not-a-directory'
    blocked.write_text('x', encoding='utf-8')
    assert logging_setup.setup_logging(blocked) is None


def test_formatter_redacts_standalone_known_secret():
    formatter = logging_setup.RedactingFormatter(secrets=('known-secret-value',))
    record = logging.LogRecord('mindock.test', logging.INFO, __file__, 1,
                               'value is known-secret-value', None, None)
    formatted = formatter.format(record)
    assert 'known-secret-value' not in formatted
    assert logging_setup.REDACTED in formatted


# ── B1.1/P2-7: settings.json 中的模型 Key 与 URL 查询参数也必须脱敏 ──


def test_settings_model_keys_are_redacted_from_disk(tmp_path):
    """模型 Key 只存 settings.json（不在环境变量），日志仍要整串替换。"""
    _write_settings(tmp_path, {
        'fast': {'api_key': 'sk-fast-settings-secret-1'},
        'reasoning': {'api_key': 'sk-reasoning-settings-secret-2'},
        'web': {'api_key': 'sk-web-settings-secret-3'},
        'model_profiles': [{'api_key': 'sk-profile-settings-secret-4'}],
    })
    path = logging_setup.setup_logging(tmp_path)
    _log_warning('provider fast=%s', 'sk-fast-settings-secret-1')
    _log_warning('provider reasoning=%s', 'sk-reasoning-settings-secret-2')
    _log_warning('provider web=%s', 'sk-web-settings-secret-3')
    _log_warning('provider profile=%s', 'sk-profile-settings-secret-4')
    text = path.read_text(encoding='utf-8')
    for secret in ('sk-fast-settings-secret-1', 'sk-reasoning-settings-secret-2',
                   'sk-web-settings-secret-3', 'sk-profile-settings-secret-4'):
        assert secret not in text
    assert logging_setup.REDACTED in text
    assert 'provider fast=' in text


def test_url_query_secrets_are_redacted_but_siblings_survive(tmp_path):
    """?key= / &token= 保留参数名、只脱敏值，且不吞掉后续参数。"""
    path = logging_setup.setup_logging(tmp_path)
    _log_warning('GET https://provider.example/v1/chat'
                 '?key=url-key-secret-1&mode=fast&token=url-token-secret-2&x=1')
    text = path.read_text(encoding='utf-8')
    assert 'url-key-secret-1' not in text
    assert 'url-token-secret-2' not in text
    assert '?key=[redacted]' in text
    assert '&token=[redacted]' in text
    assert 'mode=fast' in text
    assert 'x=1' in text
    # 普通 URL 与普通文本不受影响。
    assert 'GET https://provider.example/v1/chat' in text


def test_runtime_registered_and_refreshed_secrets_are_redacted(tmp_path):
    """设置页新保存的 Key：register_secret() 立即生效，refresh() 重读磁盘。"""
    path = logging_setup.setup_logging(tmp_path)
    logging_setup.register_secret('runtime-registered-secret-1')
    _log_warning('new key %s', 'runtime-registered-secret-1')
    _write_settings(tmp_path, {'fast': {'api_key': 'sk-settings-late-secret-9'}})
    logging_setup.refresh()
    _log_warning('saved key %s', 'sk-settings-late-secret-9')
    text = path.read_text(encoding='utf-8')
    assert 'runtime-registered-secret-1' not in text
    assert 'sk-settings-late-secret-9' not in text
    assert logging_setup.REDACTED in text


def test_rotation_keeps_private_files_and_redacts_backups(tmp_path, monkeypatch):
    monkeypatch.setattr(logging_setup, 'MAX_BYTES', 512)
    secret = 'session-rotation-secret-123456'
    monkeypatch.setenv('MINDOCK_SESSION_TOKEN', secret)
    path = logging_setup.setup_logging(tmp_path)
    for _ in range(20):
        _log_warning('padding %s %s', 'x' * 80, secret)
    _flush()
    logs = path.parent
    rotated = logs / 'mindock.log.1'
    assert path.exists()
    assert rotated.exists()
    for item in (path, rotated):
        assert stat.S_IMODE(item.stat().st_mode) == 0o600
        assert secret not in item.read_text(encoding='utf-8', errors='ignore')
