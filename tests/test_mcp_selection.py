import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent import chat
from app.api import routes
from app.integrations import mcp_bridge as bridge
from app.memory import db


def server(server_id, *, workspace='studypal', course=None, enabled=True, trusted=True,
           network=False, public_data=False, tool_name='lookup'):
    return {
        'id': server_id,
        'enabled': enabled,
        'trusted': trusted,
        'command': sys.executable,
        'args': [str(Path(__file__).parent / 'fixtures/mcp_server.py')],
        'workspace': workspace,
        'course': course,
        'network': network,
        'public_data': public_data,
        'timeout_seconds': 2,
        'tools': {tool_name: {'read_only': True, 'description': server_id}},
    }


@pytest.fixture(autouse=True)
def mcp_config(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'APP_DIR', tmp_path)
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'isolated.db')
    monkeypatch.setattr(chat, 'build_messages', lambda *args: [
        {'role': 'system', 'content': 'Synthetic test'}, {'role': 'user', 'content': 'question'}])
    path = tmp_path / 'extensions/mcp.json'
    path.parent.mkdir()

    def write(servers):
        path.write_text(json.dumps({'servers': servers}), encoding='utf-8')

    return write


def test_server_ids_reserve_selection_keywords_and_reject_non_strings():
    with pytest.raises(ValueError):
        bridge.validate_servers([server('auto')])
    with pytest.raises(ValueError):
        bridge.validate_servers([server('none')])
    with pytest.raises(ValueError):
        bridge.discover('studypal', server_id=[])  # type: ignore[arg-type]


def test_none_does_not_read_config_or_start_mcp(mcp_config, monkeypatch):
    mcp_config([server('alpha')])
    monkeypatch.setattr(bridge, '_servers', lambda: (_ for _ in ()).throw(AssertionError('must not discover')))
    monkeypatch.setattr(bridge, '_exchange', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('must not launch')))

    result = bridge.discover('studypal', server_id='none')

    assert result.tools == []
    assert result.errors == []
    assert result.selection['requested'] == 'none'
    assert result.selection['selected'] == []


def test_explicit_selection_is_exact_and_never_falls_back(mcp_config, monkeypatch):
    mcp_config([server('alpha'), server('alpha_backup')])
    calls = []

    async def exchange(selected, name=None, arguments=None):
        calls.append(selected['id'])
        return [{'name': 'lookup', 'inputSchema': {'type': 'object'},
                 'annotations': {'readOnlyHint': True}}]

    monkeypatch.setattr(bridge, '_exchange', exchange)
    result = bridge.discover('studypal', server_id='alpha')

    assert calls == ['alpha']
    assert [tool['function']['name'] for tool in result.tools] == ['mcp_alpha_lookup']
    assert result.selection['selected'] == ['alpha']


@pytest.mark.parametrize('requested', ['missing', 'alpha'])
def test_explicit_unavailable_or_out_of_scope_is_safe_error_without_process(mcp_config, monkeypatch, requested):
    mcp_config([server('alpha', enabled=requested == 'alpha', workspace='other')])
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError('must not launch an unqualified server')

    monkeypatch.setattr(bridge, '_exchange', forbidden)
    result = bridge.discover('studypal', server_id=requested)

    assert result.tools == []
    assert result.errors
    assert '指定' in result.errors[0]
    assert calls == []
    assert result.selection['selected'] == []


def test_auto_reports_all_actual_servers_and_isolated_same_named_tools(mcp_config, monkeypatch):
    mcp_config([server('alpha'), server('beta')])

    async def exchange(selected, name=None, arguments=None):
        return [{'name': 'lookup', 'inputSchema': {'type': 'object'},
                 'annotations': {'readOnlyHint': True}}]

    monkeypatch.setattr(bridge, '_exchange', exchange)
    result = bridge.discover('studypal', server_id=None)

    assert {tool['function']['name'] for tool in result.tools} == {
        'mcp_alpha_lookup', 'mcp_beta_lookup'
    }
    assert result.selection['requested'] == 'auto'
    assert result.selection['selected'] == ['alpha', 'beta']


def test_existing_session_rechecks_revocation_and_reports_actual_call_server(mcp_config, monkeypatch):
    mcp_config([server('alpha')])

    async def exchange(selected, name=None, arguments=None):
        if name is None:
            return [{'name': 'lookup', 'inputSchema': {'type': 'object'},
                     'annotations': {'readOnlyHint': True}}]
        return {'text': selected['id']}

    monkeypatch.setattr(bridge, '_exchange', exchange)
    session = bridge.discover('studypal', server_id='alpha')
    mcp_config([server('alpha', enabled=False)])

    result = session.call('mcp_alpha_lookup', {})

    assert 'error' in result
    assert session.selection['selected'] == ['alpha']


def test_chat_forwards_selection_and_adds_selection_and_call_trace(monkeypatch):
    session = bridge.ToolSet('studypal', None, 'off', server_id='alpha')
    session.tools = [{'type': 'function', 'function': {
        'name': 'mcp_alpha_lookup', 'description': 'test',
        'parameters': {'type': 'object'}}}]
    session.bindings['mcp_alpha_lookup'] = ({'id': 'alpha'}, 'lookup', {}, {}, {})
    monkeypatch.setattr(bridge, 'discover', lambda *args, **kwargs: session)

    def fake_model(tier, messages, tools, executor, **kwargs):
        assert executor('mcp_alpha_lookup', {}) == {'text': 'ok'}
        return 'answer', [], ''

    monkeypatch.setattr(session, 'call', lambda name, args: {'text': 'ok'})
    with patch.object(chat.llm, 'chat_with_tools', side_effect=fake_model):
        result = chat.answer('question', [], web_mode='off', mcp_server_id='alpha')

    assert result['mcp_selection']['requested'] == 'alpha'
    assert result['trace'][0]['type'] == 'mcp_selection'
    assert result['trace'][1]['server_id'] == 'alpha'
    assert result['trace'][1]['status'] == 'done'


def test_api_non_stream_and_stream_forward_mcp_server_id(monkeypatch):
    app = FastAPI()
    app.include_router(routes.router)
    body = routes.AskIn(question='question', web_mode='off', mcp_server_id='alpha')
    seen = []

    def fake_agent(request, progress=None):
        seen.append(request.mcp_server_id)
        return {'ok': True, 'text': 'ok', 'error': '', 'hits': [], 'agenda': None,
                'manual_results': [], 'web_results': [], 'web_error': '', 'web_query': '',
                'trace': [], 'mcp_selection': {'requested': request.mcp_server_id, 'selected': ['alpha']}}

    monkeypatch.setattr(routes, '_agent_ask', fake_agent)
    with TestClient(app) as client:
        response = client.post('/api/agent/ask', json=body.model_dump())
        assert response.status_code == 200
        assert response.json()['mcp_selection']['selected'] == ['alpha']

        stream = client.post('/api/agent/ask/stream', json=body.model_dump())
        assert stream.status_code == 200
        assert '"selected": ["alpha"]' in stream.text

    assert seen == ['alpha', 'alpha']


@pytest.mark.parametrize('selection', [None, 'auto'])
def test_null_and_auto_discover_all_eligible_servers(mcp_config, monkeypatch, selection):
    mcp_config([server('alpha'), server('beta')])
    visited = []

    async def exchange(selected, name=None, arguments=None):
        visited.append(selected['id'])
        return [{'name': 'lookup', 'inputSchema': {'type': 'object'},
                 'annotations': {'readOnlyHint': True}}]

    monkeypatch.setattr(bridge, '_exchange', exchange)
    result = bridge.discover('studypal', server_id=selection)

    assert visited == ['alpha', 'beta']
    assert result.selection == {'requested': 'auto', 'selected': ['alpha', 'beta'], 'status': 'available'}


@pytest.mark.parametrize('change,workspace,course,mode', [
    ({}, 'lifepal', None, 'auto'),
    ({'course': 'synthetic_course'}, 'studypal', None, 'auto'),
    ({'course': 'synthetic_course'}, 'studypal', 'other_course', 'auto'),
    ({'enabled': False}, 'studypal', None, 'auto'),
    ({'trusted': False}, 'studypal', None, 'auto'),
    ({'network': True}, 'studypal', None, 'off'),
    ({'tools': {'lookup': {'read_only': False}}}, 'studypal', None, 'auto'),
    ({'tools': {}}, 'studypal', None, 'auto'),
])
def test_selected_service_respects_policy_before_start(mcp_config, monkeypatch, change, workspace, course, mode):
    mcp_config([{**server('alpha'), **change}, server('beta', workspace=workspace, course=course)])
    launches = []

    async def exchange(*args, **kwargs):
        launches.append(True)
        raise AssertionError('must not start')

    monkeypatch.setattr(bridge, '_exchange', exchange)
    result = bridge.discover(workspace, course, mode, server_id='alpha')

    assert result.tools == []
    assert result.errors
    assert launches == []
    assert result.selection['status'] == 'unavailable'


@pytest.mark.parametrize('invalid', [True, False, 1, 1.0, [], {}, '', 'AUTO', ' alpha', 'alpha ',
                                         '../alpha', 'a' * 21, 'a\n'])
@pytest.mark.parametrize('endpoint', ['/api/agent/ask', '/api/agent/ask/stream'])
def test_api_rejects_invalid_selection_without_discovery(monkeypatch, invalid, endpoint):
    app = FastAPI()
    app.include_router(routes.router)
    with patch.object(bridge, 'discover') as discover, patch.object(routes, '_agent_ask') as ask:
        with TestClient(app) as client:
            result = client.post(endpoint, json={'question': 'question', 'mcp_server_id': invalid})

    assert result.status_code == 422
    discover.assert_not_called()
    ask.assert_not_called()


@pytest.mark.parametrize('payload', [{}, {'mcp_server_id': None}, {'mcp_server_id': 'auto'}])
def test_api_normalizes_automatic_selection(payload):
    assert routes.AskIn(question='question', **payload).mcp_server_id == 'auto'


def test_chat_none_never_discovers_and_keeps_builtin_tools(monkeypatch):
    def fake_model(tier, messages, tools, executor, **kwargs):
        names = [tool['function']['name'] for tool in tools]
        assert 'search_memory' in names
        assert 'query_agenda' in names
        assert not any(name.startswith('mcp_') for name in names)
        return 'answer', [], ''

    with patch.object(bridge, 'discover') as discover, \
         patch.object(chat.llm, 'chat_with_tools', side_effect=fake_model):
        result = chat.answer('question', [], web_mode='off', mcp_server_id='none')

    discover.assert_not_called()
    assert result['mcp_selection'] == {'requested': 'none', 'selected': [], 'status': 'disabled'}
    assert result['trace'] == [{'type': 'mcp_selection', **result['mcp_selection'], 'errors': []}]


@pytest.mark.parametrize('change', [{'enabled': False}, {'trusted': False}, {'workspace': 'other'},
                                  {'course': 'other'}, {'network': True}, {'tools': {}},
                                  {'args': ['changed']}])
def test_selected_service_revocation_blocks_cached_repeat(mcp_config, monkeypatch, change):
    original = server('alpha')
    mcp_config([original, server('beta')])
    calls = []

    async def exchange(selected, name=None, arguments=None):
        if name is None:
            return [{'name': 'lookup', 'inputSchema': {'type': 'object'},
                     'annotations': {'readOnlyHint': True}}]
        calls.append(selected['id'])
        return {'text': selected['id']}

    def fake_model(tier, messages, tools, executor, **kwargs):
        assert executor('mcp_alpha_lookup', {})['text'] == 'alpha'
        mcp_config([{**original, **change}, server('beta')])
        assert 'error' in executor('mcp_alpha_lookup', {})
        return 'answer', [], ''

    monkeypatch.setattr(bridge, '_exchange', exchange)
    monkeypatch.setattr(chat.llm, 'chat_with_tools', fake_model)
    result = chat.answer('question', [], web_mode='off', mcp_server_id='alpha')

    assert calls == ['alpha']
    assert result['trace'][-1]['status'] == 'error'
    assert result['trace'][-1]['server_id'] == 'alpha'


@pytest.mark.parametrize('endpoint', ['/api/agent/ask', '/api/agent/ask/stream'])
def test_api_runs_chat_with_four_services_and_selects_only_fourth(mcp_config, monkeypatch, endpoint):
    db.apply_schema()
    mcp_config([server(name) for name in ('alpha', 'beta', 'gamma', 'delta')])
    visited = []
    calls = []

    async def exchange(selected, name=None, arguments=None):
        if name is None:
            visited.append(selected['id'])
            return [{'name': 'lookup', 'inputSchema': {'type': 'object'},
                     'annotations': {'readOnlyHint': True}}]
        calls.append(selected['id'])
        return {'text': selected['id']}

    def fake_model(tier, messages, tools, executor, **kwargs):
        names = [tool['function']['name'] for tool in tools if tool['function']['name'].startswith('mcp_')]
        assert names == ['mcp_delta_lookup']
        assert executor(names[0], {})['text'] == 'delta'
        return 'synthetic answer', [], ''

    monkeypatch.setattr(bridge, '_exchange', exchange)
    monkeypatch.setattr(chat.llm, 'chat_with_tools', fake_model)
    monkeypatch.setattr(routes.ContextCompiler, 'build_context', lambda *args, **kwargs: None)
    monkeypatch.setattr(routes.llm, 'available', lambda *args: True)
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as client:
        response = client.post(endpoint, json={'question': 'question', 'web_mode': 'off', 'mcp_server_id': 'delta'})

    assert response.status_code == 200
    if endpoint.endswith('/stream'):
        events = [json.loads(line.removeprefix('data: ')) for line in response.text.splitlines() if line.startswith('data: ')]
        assert events[0]['type'] == 'mcp_selection'
        result = next(event['data'] for event in events if event['type'] == 'result')
    else:
        result = response.json()
    assert visited == ['delta']
    assert calls == ['delta']
    assert result['mcp_selection'] == {'requested': 'delta', 'selected': ['delta'], 'status': 'available'}
    assert result['trace'][-1]['server_id'] == 'delta'
    assert result['mcp_errors'] == []


def test_explicit_service_failure_is_sanitized_and_does_not_try_another(mcp_config, monkeypatch):
    mcp_config([server('alpha'), server('beta')])
    visited = []

    async def exchange(selected, name=None, arguments=None):
        visited.append(selected['id'])
        raise RuntimeError('synthetic-private-provider-error')

    monkeypatch.setattr(bridge, '_exchange', exchange)
    result = bridge.discover('studypal', server_id='alpha')

    assert visited == ['alpha']
    assert result.tools == []
    assert result.errors
    assert 'synthetic-private' not in str(result.errors)


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='optional MCP SDK')
def test_real_stdio_fourth_service_selection_and_same_named_tools(mcp_config):
    mcp_config([server(name) for name in ('alpha', 'beta', 'gamma', 'delta')])
    result = bridge.discover('studypal', server_id='delta')

    assert [tool['function']['name'] for tool in result.tools] == ['mcp_delta_lookup']
    assert result.selection['selected'] == ['delta']
    assert result.call('mcp_delta_lookup', {'query': 'selection'})['text'] == 'selection value'
    assert 'error' in result.call('mcp_alpha_lookup', {'query': 'selection'})

