"""Verifier tests; these do NOT constitute frozen-backend acceptance."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
from unittest.mock import Mock

import pytest

from scripts import verify_mcp_desktop as verify


def test_environment_is_private_and_does_not_inherit_credentials(tmp_path, monkeypatch):
    for name in ('OPENAI_API_KEY', 'PYTHONPATH', 'MINDOCK_RUNTIME_LOCKED', 'MINDOCK_SESSION_TOKEN'):
        monkeypatch.setenv(name, 'private-existing-value')
    env = verify.environment(tmp_path, 'synthetic-session', 'synthetic-fixture')
    assert env['CYBERPAL_DATA_DIR'] == str(tmp_path)
    assert env['HOME'] == str(tmp_path)
    assert env['MINDOCK_SESSION_TOKEN'] == 'synthetic-session'
    assert 'private-existing-value' not in json.dumps(env)
    assert 'PYTHONPATH' not in env and 'OPENAI_API_KEY' not in env


def test_source_script_is_never_accepted_as_frozen_candidate(tmp_path):
    backend = tmp_path / 'Contents/Resources/backend/mindock-backend'
    backend.parent.mkdir(parents=True)
    backend.write_text('#!/usr/bin/python3\nprint("not frozen")\n')
    backend.chmod(0o700)
    report = verify.verify(tmp_path)
    assert report['ok'] is False
    assert report['failure_code'] == 'candidate_not_macho'
    assert str(tmp_path) not in json.dumps(report)


def test_missing_candidate_cli_emits_only_safe_json(tmp_path):
    report = verify.verify(tmp_path / 'private-candidate-name.app')
    assert report['failure_code'] == 'candidate_missing'
    assert 'private-candidate-name' not in json.dumps(report)
    assert report['checks'] == []


def test_stop_kills_process_group_on_timeout_and_reports_failure(tmp_path, monkeypatch):
    backend = verify.Backend(tmp_path / 'backend', tmp_path, {}, None)
    process = Mock(pid=987654, returncode=-signal.SIGKILL)
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired('private-command', 15), None]
    backend.process = process
    kill = Mock()
    monkeypatch.setattr(os, 'killpg', kill)
    with pytest.raises(verify.VerificationError):
        backend.stop()
    process.terminate.assert_called_once()
    assert process.wait.call_count == 2
    kill.assert_called_with(987654, signal.SIGKILL)
    assert backend.process is None


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='optional SDK')
def test_api_sequence_against_real_sdk_fixtures_in_source_test_only(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.memory import db
    from app.api.mcp_routes import router
    from app.api.model_routes import router as model_router
    from app.desktop import DesktopAccessGuard
    from app.settings import store

    monkeypatch.setattr(db, 'APP_DIR', tmp_path / 'app')
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'app/test.sqlite')
    monkeypatch.setattr(store, 'SETTINGS_PATH', tmp_path / 'app/settings.json')
    db.apply_schema()
    spec = importlib.util.spec_from_file_location('http_fixture_test',
        Path(__file__).parent / 'fixtures/mcp_http_server.py')
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    app = FastAPI()
    app.include_router(router, prefix='/api')
    app.include_router(model_router, prefix='/api')
    app.add_middleware(DesktopAccessGuard, token='synthetic-session')
    starts, stops, checks = [], [], []

    class SourceOnlyAdapter:
        def request(self, path, body=None, method='GET', *, origin=None, authenticated=True):
            headers = {'Origin': origin or 'http://127.0.0.1'}
            if authenticated:
                headers['X-MinDock-Session'] = 'synthetic-session'
            result = client.request(method, path, json=body, headers=headers)
            return result.status_code, result.json()

        ok = verify.Backend.ok

        def start(self):
            starts.append(True)

        def stop(self):
            stops.append(True)

    with fixture.running_server(token='synthetic-fixture') as peer, \
            TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 12345)) as client:
        monkeypatch.setenv('MCP_DESKTOP_FIXTURE_TOKEN', peer.token)
        verify.exercise(SourceOnlyAdapter(), verify.fixtures(peer.url), peer, checks.append)
        verify.exercise_models(SourceOnlyAdapter(), checks.append)
    assert len(starts) == 3 and len(stops) == 3
    assert 'test_stdio' in checks and 'test_streamable_http' in checks
    assert 'cross_origin_and_session_denied' in checks
    assert 'deletion_survives_restart' in checks
    assert 'model_profiles_restart_and_masking' in checks
    assert json.loads((tmp_path / 'app/extensions/mcp.json').read_text()) == {'servers': []}
