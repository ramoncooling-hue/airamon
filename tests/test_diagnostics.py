"""B2/T2.18a: 数据与隐私诊断（/api/diagnostics + 脱敏诊断包）。

安全契约：只返回相对名称/计数/摘要；绝不返回绝对路径、settings 明文、日志原文
或数据库内容。所有降级路径（无库/无备份/无日志/坏库）都必须返回稳定形状。
"""

from __future__ import annotations

import json
import sqlite3
import zipfile

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api import routes
from app.api.mcp_routes import _ORIGIN_ERROR, verify_local_request
from app.maintenance import diagnostics
from app.memory import db

SETTINGS_MODEL_KEY = 'sk-settings-diagnostic-secret-1'
URL_QUERY_SECRET = 'url-diagnostic-secret-2'
BEARER_SECRET = 'bearer-diagnostic-token-3'
LOCAL_BASE = 'http://127.0.0.1:8765'


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    root = tmp_path / 'data-root'
    monkeypatch.setattr(db, 'DATA_DIR', root)
    monkeypatch.setattr(db, 'APP_DIR', root / 'app')
    monkeypatch.setattr(db, 'DB_PATH', root / 'app' / 'banxue.db')
    db.apply_schema()
    return root


def _write_settings(root, payload=None):
    payload = payload or {
        'fast': {'api_key': SETTINGS_MODEL_KEY, 'base_url': 'https://api.example/v1'},
        'reasoning': {'api_key': 'sk-reasoning-diagnostic-secret'},
        'web': {'api_key': 'sk-web-diagnostic-secret'},
        'model_profiles': [{'api_key': 'sk-profile-diagnostic-secret'}],
    }
    target = root / 'app' / 'settings.json'
    target.write_text(json.dumps(payload), encoding='utf-8')
    return target


def _write_log(root):
    log = root / 'app' / 'logs' / 'mindock.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        '2026-10-17 21:00:00,000 WARNING mindock.test provider key %s failed' % SETTINGS_MODEL_KEY,
        '2026-10-17 21:00:01,000 ERROR mindock.test GET https://api.example/v1'
        '?key=%s&mode=fast' % URL_QUERY_SECRET,
        '2026-10-17 21:00:02,000 WARNING mindock.test Authorization: Bearer %s' % BEARER_SECRET,
        '2026-10-17 21:00:03,000 INFO mindock.test normal line without secrets',
    ]
    log.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return log


def _local_client(app):
    return TestClient(app, base_url=LOCAL_BASE, client=('127.0.0.1', 43210))


def test_summary_shape_and_path_redaction(data_root, tmp_path):
    payload = diagnostics.summary()
    assert payload['format_version'] == diagnostics.EXPORT_FORMAT_VERSION
    assert set(payload) >= {'generated_at', 'app', 'data_root', 'schema',
                            'capabilities', 'backups', 'logs', 'workspaces', 'recovery'}
    assert payload['app']['version']
    assert payload['app']['platform']

    assert payload['data_root']['basename'] == 'data-root'
    assert isinstance(payload['data_root']['custom'], bool)
    assert payload['data_root']['database_present'] is True
    assert payload['data_root']['database_size_bytes'] > 0

    assert payload['schema']['migrations'] >= 1
    assert payload['schema']['user_version'] is not None
    assert payload['schema']['compatible'] is True
    assert payload['schema']['latest']

    assert payload['capabilities']['sqlite_version']
    assert payload['capabilities']['fts5'] is True
    assert payload['capabilities']['trigram'] is True
    assert payload['capabilities']['search_fts_table'] is True
    assert payload['capabilities']['search_fts_readable'] is True

    assert payload['workspaces']['count'] == 3
    assert {item['workspace'] for item in payload['workspaces']['items']} == {
        'studypal', 'lifepal', 'workpal'}
    for item in payload['workspaces']['items']:
        assert isinstance(item['kp'], int)
        assert isinstance(item['documents'], int)
        assert isinstance(item['notes'], int)

    text = json.dumps(payload, ensure_ascii=False)
    assert str(data_root) not in text
    assert str(tmp_path) not in text
    assert '/Users' not in text
    assert 'api_key' not in text
    # 无备份、无日志也要给出可读的降级与提示。
    assert payload['backups']['count'] == 0
    assert payload['backups']['items'] == []
    assert payload['logs']['present'] is False
    assert payload['logs']['entries'] == []
    assert payload['recovery']['backup_available'] is False
    assert payload['recovery']['hints']


def test_summary_redacts_settings_key_url_secret_and_bearer(data_root):
    _write_settings(data_root)
    _write_log(data_root)
    payload = diagnostics.summary()
    assert payload['logs']['present'] is True
    assert payload['logs']['levels'] == {'ERROR': 1, 'WARNING': 2}
    entries = payload['logs']['entries']
    assert len(entries) == 3
    assert {entry['level'] for entry in entries} == {'ERROR', 'WARNING'}
    assert all(entry['time'] and entry['logger'] for entry in entries)

    text = json.dumps(payload, ensure_ascii=False)
    for secret in (SETTINGS_MODEL_KEY, URL_QUERY_SECRET, BEARER_SECRET):
        assert secret not in text
    assert diagnostics._redact('placeholder') == 'placeholder'  # formatter stays stable
    messages = ' '.join(entry['message'] for entry in entries)
    assert '[redacted]' in messages
    # INFO 行不进入错误摘要。
    assert 'normal line without secrets' not in text
    # URL 的兄弟参数不被吞掉。
    assert 'mode=fast' in messages


def test_backup_list_exposes_name_time_and_size_only(data_root):
    backups = data_root / 'app' / 'backups'
    backups.mkdir(parents=True)
    legacy = backups / 'before-upgrade-abc.db'
    legacy.write_bytes(b'x' * 100)
    archive = backups / 'before-seed-20261017.zip'
    archive.write_bytes(b'y' * 50)
    (backups / 'note.txt').write_text('ignored', encoding='utf-8')
    (backups / 'nested').mkdir()

    payload = diagnostics.summary()
    assert payload['backups']['directory_present'] is True
    assert payload['backups']['count'] == 2
    items = payload['backups']['items']
    assert {item['name'] for item in items} == {'before-upgrade-abc.db',
                                                'before-seed-20261017.zip'}
    for item in items:
        assert set(item) == {'name', 'modified_at', 'size_bytes'}
        assert item['size_bytes'] in (50, 100)
    assert payload['backups']['latest_at']
    text = json.dumps(payload, ensure_ascii=False)
    assert str(backups) not in text


def test_summary_degrades_when_database_is_unavailable(data_root, monkeypatch):
    def broken():
        raise sqlite3.OperationalError('database is locked')

    monkeypatch.setattr(db, 'connect', broken)
    payload = diagnostics.summary()
    assert payload['schema']['migrations'] is None
    assert payload['schema']['user_version'] is None
    # 兼容性探测直接读库文件，锁冲突时可以是 True/False/None，但形状必须稳定。
    assert payload['schema']['compatible'] in (True, False, None)
    assert payload['capabilities']['search_fts_table'] is False
    assert payload['workspaces']['count'] == 0
    assert payload['logs']['present'] is False


def test_export_writes_redacted_zip_without_database_or_settings(data_root):
    _write_settings(data_root)
    _write_log(data_root)
    result = diagnostics.export()
    assert result['ok'] is True
    assert result['size_bytes'] > 0
    assert result['contents'] == ['diagnostics.json', 'logs-summary.json']
    name = result['filename']
    assert name.startswith('diagnostics-') and name.endswith('.zip')

    target = data_root / 'app' / 'diagnostics' / name
    assert target.is_file()
    import stat as stat_module
    assert stat_module.S_IMODE(target.stat().st_mode) == 0o600

    with zipfile.ZipFile(target) as archive:
        assert set(archive.namelist()) == {'diagnostics.json', 'logs-summary.json'}
        diagnostics_text = archive.read('diagnostics.json').decode('utf-8')
        logs_text = archive.read('logs-summary.json').decode('utf-8')
    body = json.loads(diagnostics_text)
    assert body['format_version'] == diagnostics.EXPORT_FORMAT_VERSION
    assert body['diagnostics']['data_root']['basename'] == 'data-root'
    logs = json.loads(logs_text)
    assert logs['present'] is True
    assert len(logs['entries']) <= diagnostics.MAX_LOG_ENTRIES

    combined = diagnostics_text + logs_text
    for secret in (SETTINGS_MODEL_KEY, URL_QUERY_SECRET, BEARER_SECRET):
        assert secret not in combined
    assert str(data_root) not in combined
    assert '/Users' not in combined
    assert 'banxue.db' not in combined
    assert 'settings.json' not in combined


def test_export_size_limit_leaves_no_partial_file(data_root, monkeypatch):
    monkeypatch.setattr(diagnostics, 'MAX_EXPORT_BYTES', 10)
    with pytest.raises(ValueError):
        diagnostics.export()
    directory = data_root / 'app' / 'diagnostics'
    assert not list(directory.glob('*.zip'))
    assert not list(directory.glob('*.tmp'))


def test_routes_get_and_export(data_root):
    app = FastAPI()
    app.include_router(routes.router, dependencies=[Depends(verify_local_request)])
    client = _local_client(app)
    summary = client.get('/api/diagnostics')
    assert summary.status_code == 200
    payload = summary.json()
    assert payload['data_root']['basename'] == 'data-root'
    assert str(data_root) not in summary.text

    exported = client.post('/api/diagnostics/export')
    assert exported.status_code == 200
    result = exported.json()
    assert result['ok'] is True
    assert (data_root / 'app' / 'diagnostics' / result['filename']).is_file()
    assert str(data_root) not in exported.text


def test_export_write_is_guarded_for_cross_origin_pages(data_root):
    """浏览器跨源页面不能触发写诊断包（全局同源守卫）。"""
    from app import main

    client = _local_client(main.app)
    blocked = client.post('/api/diagnostics/export',
                          headers={'origin': 'https://evil.example'})
    assert blocked.status_code == 403
    assert blocked.json()['detail'] == _ORIGIN_ERROR
    allowed = client.post('/api/diagnostics/export')
    assert allowed.status_code == 200
    assert allowed.json()['ok'] is True
