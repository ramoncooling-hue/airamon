import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent import chat, llm
from app.api import routes
from app.integrations import mcp_bridge as bridge
from app.memory import db


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'APP_DIR', tmp_path / 'app')
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'app/test.db')
    db.apply_schema()
    conn = db.connect()
    try:
        conn.execute("INSERT INTO courses(name) VALUES('Accounting')")
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(llm, 'available', lambda *args: True)


def server(key, **changes):
    return dict({
        'id': key, 'enabled': True, 'trusted': True, 'transport': 'stdio',
        'command': sys.executable,
        'args': [str(Path(__file__).parent / 'fixtures/mcp_server.py')],
        'workspace': 'studypal', 'course': None, 'network': False,
        'public_data': False, 'timeout_seconds': 3,
        'tools': {'lookup': {'read_only': True}},
    }, **changes)


def configure(*servers):
    path = bridge.config_path()
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({'servers': servers}))


def tool_definition(read_only=True):
    return [{'name': 'lookup', 'inputSchema': {
        'type': 'object', 'properties': {'query': {'type': 'string'}},
        'required': ['query'], 'additionalProperties': False,
    }, 'annotations': {'readOnlyHint': read_only}}]


@pytest.mark.parametrize('selection', ['second', 'third', 'fourth'])
def test_only_selected_service_is_exposed_and_invoked(isolated, monkeypatch, selection):
    configure(*(server(key) for key in ['first', 'second', 'third', 'fourth']))
    seen = []

    async def exchange(value, name=None, arguments=None):
        seen.append((value['id'], name))
        return {'text': value['id']} if name else tool_definition()

    monkeypatch.setattr(bridge, '_exchange', exchange)
    result = bridge.discover('studypal', server_id=selection)
    assert [t['function']['name'] for t in result.tools] == [f'mcp_{selection}_lookup']
    assert result.call(f'mcp_{selection}_lookup', {'query': 'test'})['text'] == selection
    assert 'error' in result.call('mcp_first_lookup', {'query': 'test'})
    assert seen == [(selection, None), (selection, 'lookup')]


@pytest.mark.parametrize('changes,workspace,course,mode', [
    ({'enabled': False}, 'studypal', None, 'auto'),
    ({'trusted': False}, 'studypal', None, 'auto'),
    ({}, 'lifepal', None, 'auto'),
    ({}, 'studypal', 'Accounting', 'auto'),
    ({'course': 'Accounting'}, 'studypal', None, 'auto'),
    ({'network': True}, 'studypal', None, 'off'),
    ({}, 'studypal', None, 'invalid'),
    ({}, 'studypal', '', 'auto'),
])
def test_selected_scope_failure_is_diagnosed_before_io(
        isolated, monkeypatch, changes, workspace, course, mode):
    configure(server('selected', **changes), server('other', public_data=True))
    seen = []

    async def exchange(*args):
        seen.append(True)
        return tool_definition()

    monkeypatch.setattr(bridge, '_exchange', exchange)
    result = bridge.discover(workspace, course, mode, server_id='selected')
    assert result.tools == []
    assert result.errors
    assert seen == []


@pytest.mark.parametrize('policy,remote_readonly', [(False, True), (True, False)])
def test_both_local_and_remote_readonly_are_required(isolated, monkeypatch, policy, remote_readonly):
    configure(server('selected', tools={'lookup': {'read_only': policy}}), server('other'))

    async def exchange(*args):
        return tool_definition(remote_readonly)

    monkeypatch.setattr(bridge, '_exchange', exchange)
    result = bridge.discover('studypal', server_id='selected')
    assert result.tools == []
    assert result.errors


@pytest.mark.parametrize('mode', ['auto', 'on', 'off'])
def test_public_scope_stays_within_workspace_and_network_permissions(isolated, monkeypatch, mode):
    configure(server('selected', public_data=True, network=True))

    async def exchange(*args):
        return tool_definition()

    monkeypatch.setattr(bridge, '_exchange', exchange)
    assert bool(bridge.discover('studypal', 'Accounting', mode, server_id='selected').tools) is (mode != 'off')
    assert bridge.discover('lifepal', 'Accounting', mode, server_id='selected').tools == []


@pytest.mark.parametrize('change', [None, {'enabled': False}, {'trusted': False},
    {'workspace': 'lifepal'}, {'course': 'Accounting'}, {'network': True},
    {'tools': {}}, {'tools': {'lookup': {'read_only': False}}}])
def test_revocation_blocks_identical_cached_chat_calls(isolated, monkeypatch, change):
    original = server('selected')
    configure(original, server('other'))
    calls, observed = [], []

    async def exchange(value, name=None, arguments=None):
        if name:
            calls.append(value['id'])
            return {'text': 'before revocation'}
        return tool_definition()

    def model(tier, messages, tools, executor, **kwargs):
        observed.append(executor('mcp_selected_lookup', {'query': 'identical'}))
        configure(*([dict(copy.deepcopy(original), **change)] if change is not None else []), server('other'))
        observed.append(executor('mcp_selected_lookup', {'query': 'identical'}))
        return 'normal answer', [], ''

    monkeypatch.setattr(bridge, '_exchange', exchange)
    monkeypatch.setattr(llm, 'chat_with_tools', model)
    result = chat.answer('explain a concept', [], web_mode='off', mcp_server_id='selected')
    assert result['ok']
    assert observed[0]['text'] == 'before revocation'
    assert 'error' in observed[1]
    assert calls == ['selected']
    assert result['trace'][-1]['status'] == 'error'
    assert result['mcp_errors']


@pytest.mark.parametrize('selection', ['', 'missing', 'bad id', '../private-secret', [], {}])
def test_bad_selection_keeps_conversation_and_streams_safe_diagnostic(isolated, monkeypatch, selection):
    configure(server('other'))
    events = []

    def model(tier, messages, tools, executor, **kwargs):
        names = {t['function']['name'] for t in tools}
        assert {'search_memory', 'query_agenda', 'query_manual'} <= names
        assert not any(name.startswith('mcp_') for name in names)
        assert 'private-secret' not in str(messages)
        return 'normal answer', [], ''

    monkeypatch.setattr(llm, 'chat_with_tools', model)
    result = chat.answer('explain a concept', [], web_mode='off',
                         mcp_server_id=selection, progress=events.append)
    assert result['ok'] and result['text'] == 'normal answer'
    assert result['mcp_errors']
    assert any(e.get('type') == 'mcp_selection' and e['errors'] for e in result['trace'])
    assert any(e.get('type') == 'mcp_selection' and e['errors'] for e in events)
    assert 'private-secret' not in str(result)


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('selection,expected', [
    (None, {'mcp_first_lookup', 'mcp_second_lookup'}),
    ('auto', {'mcp_first_lookup', 'mcp_second_lookup'}),
    ('none', set()), ('second', {'mcp_second_lookup'}), ('missing', set()),
])
def test_chat_endpoints_forward_choice_and_return_diagnostics(isolated, monkeypatch, stream, selection, expected):
    configure(server('first'), server('second'))

    async def exchange(*args):
        return tool_definition()

    def model(tier, messages, tools, executor, **kwargs):
        assert {t['function']['name'] for t in tools if t['function']['name'].startswith('mcp_')} == expected
        return 'normal answer', [], ''

    monkeypatch.setattr(bridge, '_exchange', exchange)
    monkeypatch.setattr(llm, 'chat_with_tools', model)
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as client:
        response = client.post('/api/agent/ask' + ('/stream' if stream else ''), json={
            'question': 'explain a concept', 'web_mode': 'off', 'mcp_server_id': selection})
    assert response.status_code == 200
    if stream:
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        result = next(e['data'] for e in events if e['type'] == 'result')
    else:
        result = response.json()
    assert result['text'] == 'normal answer'
    assert bool(result['mcp_errors']) is (selection in ('missing', 'bad id'))


def test_chat_and_direct_test_reject_foreign_course_before_discovery(isolated, monkeypatch):
    configure(server('selected', workspace='lifepal', course='Accounting', public_data=True))
    seen = []

    async def exchange(*args):
        seen.append(True)
        return tool_definition()

    monkeypatch.setattr(bridge, '_exchange', exchange)
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 1234)) as client:
        response = client.post('/api/agent/ask', json={
            'question': 'test', 'workspace': 'lifepal', 'course': 'Accounting', 'mcp_server_id': 'selected'})
        assert response.status_code == 400
        response = client.post('/api/extensions/mcp/test', json={
            'id': 'selected', 'tool': 'lookup', 'arguments': {'query': 'test'},
            'workspace': 'lifepal', 'course': 'Accounting'})
        assert response.status_code == 422
    assert seen == []


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='optional MCP SDK')
def test_real_stdio_selection_and_deletion(isolated):
    configure(server('first'), server('second'), server('third'))
    auto = bridge.discover('studypal')
    assert len(auto.tools) == 3, auto.errors
    selected = bridge.discover('studypal', server_id='third')
    assert [t['function']['name'] for t in selected.tools] == ['mcp_third_lookup']
    assert selected.call('mcp_third_lookup', {'query': 'real protocol'})['text'] == 'real protocol value'
    configure(server('first'), server('second'))
    assert 'error' in selected.call('mcp_third_lookup', {'query': 'real protocol'})
    assert selected.errors
    assert bridge.discover('studypal', server_id='third').errors
