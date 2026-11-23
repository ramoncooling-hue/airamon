import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.memory import db
from app.integrations import mcp_settings as settings
from app.api.mcp_routes import router


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'APP_DIR', tmp_path / 'app')
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'app/test.db')
    db.apply_schema()
    app = FastAPI()
    app.include_router(router, prefix='/api')
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 12345)) as client:
        yield client


def server(**overrides):
    return dict({
        'id':'fixture', 'label':'测试资料', 'enabled':False, 'trusted':False,
        'transport':'stdio', 'command':sys.executable,
        'args':[str(Path(__file__).parent / 'fixtures/mcp_server.py')],
        'workspace':'studypal', 'course':None, 'public_data':False,
        'network':False, 'env_names':[], 'timeout_seconds':3,
        'tools':{'lookup':{'read_only':True}},
    }, **overrides)


def test_crud_persists_without_launching_and_stale_revision_preserves_data(isolated):
    initial = isolated.get('/api/extensions/mcp').json()
    with patch('app.integrations.mcp_bridge._exchange', side_effect=AssertionError('no launch')):
        result = isolated.put('/api/extensions/mcp', json={'servers':[server()], 'revision':initial['revision']})
    assert result.status_code == 200, result.text
    saved = result.json()
    assert saved['servers'][0]['label'] == '测试资料'
    assert isolated.get('/api/extensions/mcp').json()['revision'] == saved['revision']
    assert settings.config_path().stat().st_mode & 0o777 == 0o600
    stale = isolated.put('/api/extensions/mcp', json={'servers':[], 'revision':initial['revision']})
    assert stale.status_code == 409
    assert len(isolated.get('/api/extensions/mcp').json()['servers']) == 1
    result = isolated.put('/api/extensions/mcp', json={'servers':[], 'revision':saved['revision']})
    assert result.status_code == 200
    assert settings.read()['servers'] == []


@pytest.mark.parametrize('origin', ['https://evil.example', 'null', 'http://localhost', 'http://127.0.0.1.evil.test'])
def test_cross_origin_configuration_cannot_be_read_or_executed(isolated, origin):
    headers = {'origin':origin}
    assert isolated.get('/api/extensions/mcp', headers=headers).status_code == 403
    assert isolated.post('/api/extensions/mcp/probe', json={'server':server(trusted=True)}, headers=headers).status_code == 403
    assert isolated.put('/api/extensions/mcp', json={'servers':[], 'revision':'x'}, headers=headers).status_code == 403


def test_dns_rebinding_host_rejected(isolated):
    assert isolated.get('/api/extensions/mcp', headers={'host':'evil.example'}).status_code == 403


def test_invalid_scope_and_raw_credentials_never_overwrite_config(isolated):
    initial = isolated.get('/api/extensions/mcp').json()
    for candidate in [server(workspace='missing'), server(course='missing'), server(env={'KEY':'fake-secret'})]:
        result = isolated.put('/api/extensions/mcp', json={'servers':[candidate], 'revision':initial['revision']})
        assert result.status_code == 422
        assert 'fake-secret' not in result.text
    assert not settings.config_path().exists()


def test_probe_requires_trust_and_does_not_save(isolated):
    response = isolated.post('/api/extensions/mcp/probe', json={'server':server()})
    assert response.status_code == 422
    assert not settings.config_path().exists()


def test_symlink_parent_is_not_followed(isolated, tmp_path):
    external = tmp_path / 'outside'
    external.mkdir()
    (db.APP_DIR / 'extensions').symlink_to(external, target_is_directory=True)
    result = isolated.get('/api/extensions/mcp')
    assert result.status_code == 422
    assert list(external.iterdir()) == []


def test_malformed_file_is_not_silently_replaced(isolated):
    path = settings.config_path()
    path.parent.mkdir(parents=True)
    path.write_text('broken secret')
    read = isolated.get('/api/extensions/mcp')
    assert read.status_code == 422
    assert 'broken secret' not in read.text
    result = isolated.put('/api/extensions/mcp', json={'servers':[], 'revision':'anything'})
    assert result.status_code == 422
    assert path.read_text() == 'broken secret'


def test_real_probe_and_authorized_call_use_same_runtime_gates(isolated):
    pytest.importorskip('mcp')
    candidate = server(trusted=True, enabled=True)
    probe = isolated.post('/api/extensions/mcp/probe', json={'server':{**candidate, 'tools':{}}})
    assert probe.status_code == 200, probe.text
    assert probe.json()['ok'], probe.text
    tools = probe.json()['tools']
    assert any(tool['name'] == 'lookup' and tool['eligible'] for tool in tools)
    assert any(tool['name'] == 'delete' and not tool['eligible'] for tool in tools)
    assert not settings.config_path().exists()
    initial = isolated.get('/api/extensions/mcp').json()
    result = isolated.put('/api/extensions/mcp', json={'servers':[candidate], 'revision':initial['revision']})
    assert result.status_code == 200, result.text
    body = {'id':'fixture','tool':'lookup','arguments':{'query':'fixture'},'workspace':'studypal','course':None,'web_mode':'auto'}
    result = isolated.post('/api/extensions/mcp/test', json=body)
    assert result.json()['ok'], result.text
    assert 'fixture value' in result.json()['result']['text']
    for change in [{'workspace':'lifepal'}, {'course':'会计学'}, {'tool':'delete'}, {'arguments':{'query':23}}]:
        result = isolated.post('/api/extensions/mcp/test', json={**body, **change})
        assert result.status_code in (200, 422)
        assert not result.json().get('ok', False)


def test_credentials_status_only_reports_missing_names(isolated, monkeypatch):
    monkeypatch.setenv('MCP_TEST_PRESENT', 'private-value')
    candidate = server(env_names=['MCP_TEST_PRESENT', 'MCP_TEST_MISSING'])
    initial = isolated.get('/api/extensions/mcp').json()
    result = isolated.put('/api/extensions/mcp', json={'servers':[candidate], 'revision':initial['revision']})
    assert result.status_code == 200, result.text
    assert result.json()['missing_env']['fixture'] == ['MCP_TEST_MISSING']
    assert 'private-value' not in result.text


def test_foreign_client_and_simple_cross_site_requests_are_rejected(isolated):
    assert isolated.get('/api/extensions/mcp', headers={'sec-fetch-site':'cross-site'}).status_code == 403
    with TestClient(isolated.app, base_url='http://127.0.0.1', client=('192.168.1.20', 1234)) as remote:
        assert remote.get('/api/extensions/mcp').status_code == 403
    assert isolated.put('/api/extensions/mcp', content='{}', headers={'content-type':'text/plain'}).status_code == 415
    assert isolated.put('/api/extensions/mcp', content='x' * 65537, headers={'content-type':'application/json'}).status_code == 413
    nested = '[' * 15000 + '0' + ']' * 15000
    assert isolated.put('/api/extensions/mcp', content=nested, headers={'content-type':'application/json'}).status_code == 422


def test_atomic_write_failure_preserves_existing_configuration(isolated):
    initial = isolated.get('/api/extensions/mcp').json()
    saved = isolated.put('/api/extensions/mcp', json={'servers':[server()], 'revision':initial['revision']}).json()
    original = settings.config_path().read_bytes()
    with patch('app.integrations.mcp_settings.os.replace', side_effect=OSError('fake sensitive path')):
        response = isolated.put('/api/extensions/mcp', json={'servers':[], 'revision':saved['revision']})
    assert response.status_code == 422
    assert 'sensitive' not in response.text
    assert settings.config_path().read_bytes() == original
    assert list(settings.config_path().parent.glob('.mcp-*')) == []
