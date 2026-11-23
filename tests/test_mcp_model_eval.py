import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.agent import llm
from app.integrations import mcp_bridge as bridge
from app.memory import db
from scripts import verify_mcp_model as evaluation


def test_opt_in_gate_does_not_read_config_or_start_process(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('not authorized')
    monkeypatch.setattr(llm, 'available', forbidden)
    monkeypatch.setattr(bridge, '_exchange', forbidden)
    assert evaluation.verify()['outcome'] == 'not_requested'




def test_missing_config_does_not_create_personal_data(tmp_path):
    personal = tmp_path / 'personal'
    result = subprocess.run([sys.executable, evaluation.__file__, '--model-check'],
        env=dict(os.environ, CYBERPAL_DATA_DIR=str(personal)), capture_output=True, text=True, timeout=5)
    assert result.returncode == 1
    assert json.loads(result.stdout)['outcome'] == 'not_configured'
    assert not personal.exists()
    assert result.stderr == ''


@pytest.mark.parametrize('change,workspace,course,mode', [
    ({}, 'wrong', 'synthetic_course', 'off'),
    ({}, 'synthetic_mcp', 'wrong', 'off'),
    ({'enabled': False}, 'synthetic_mcp', 'synthetic_course', 'off'),
    ({'network': True}, 'synthetic_mcp', 'synthetic_course', 'off'),
    ({'trusted': False}, 'synthetic_mcp', 'synthetic_course', 'off'),
])
def test_deterministic_policy_gates_never_start_sdk(tmp_path, monkeypatch, change, workspace, course, mode):
    monkeypatch.setattr(db, 'APP_DIR', tmp_path)
    path = tmp_path / 'extensions/mcp.json'
    path.parent.mkdir()
    path.write_text(json.dumps({'servers': [{**evaluation.server_config(), **change}]}))
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError('must not launch')

    monkeypatch.setattr(bridge, '_exchange', forbidden)
    assert bridge.discover(workspace, course, mode).tools == []
    assert calls == []


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='optional MCP SDK')
@pytest.mark.parametrize('behavior,outcome', [
    ('success', 'passed'), ('skip', 'tool_not_called'), ('omit', 'marker_missing'),
    ('timeout', 'model_timeout'), ('private', 'unexpected_tool'),
])
@pytest.mark.parametrize('selection', [False, True])
def test_actual_chat_and_stdio_with_explicit_mock_model(monkeypatch, behavior, outcome, selection):
    monkeypatch.setattr(llm.settings_store, 'read_raw', lambda: {
        'fast': {'base_url': 'http://synthetic.invalid', 'model': 'test', 'api_key': 'fake-secret'}})
    before = db.APP_DIR
    requests = []

    def synthetic_completion(url, body, headers, deadline):
        requests.append(body)
        assert body['tool_choice'] == 'auto'
        assert body['stream'] is False
        assert 'fake-secret' not in json.dumps(body)
        assert not list(db.APP_DIR.glob('*.sqlite'))
        if behavior == 'timeout':
            raise llm.LLMError('provider-private-error', code='timeout')
        tool_messages = [message for message in body['messages'] if message['role'] == 'tool']
        if behavior == 'skip':
            message = {'content': 'I cannot find it'}
        elif not tool_messages:
            message = {'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': 'provider-private-call-id', 'type': 'function', 'function': {
                    'name': 'search_memory' if behavior == 'private' else evaluation.TOOL_NAME,
                    'arguments': '{"query":"synthetic"}' if behavior == 'private' else '{}'}}]}
        else:
            value = json.loads(tool_messages[-1]['content'])
            message = {'content': value.get('text', '') if behavior == 'success' else 'omitted'}
        return {'choices': [{'message': message}]}

    report = evaluation.verify(model_check=True, test_completion=synthetic_completion,
        **({'service_count': 4, 'selected_service': 'acceptance'} if selection else {}))
    assert report['outcome'] == outcome, report
    assert report['execution'] == 'deterministic_mock'
    assert report['real_model_acceptance'] is False
    assert db.APP_DIR == before
    assert report['personal_db_access'] is False
    assert requests
    if selection:
        assert report['service_count'] == 4
        assert report['selected_service'] == 'acceptance'
        assert 'unexpected' not in report['tools_exposed']
    text = json.dumps(report)
    for private in ['fake-secret', 'synthetic.invalid', 'provider-private', str(before)]:
        assert private not in text
    if behavior == 'success':
        assert report['marker_in_final_answer']
        assert report['model_selected_tool']
        assert report['invocations'][0]['status'] == 'returned_marker'
