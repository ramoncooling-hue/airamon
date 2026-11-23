"""Independent MCP acceptance contracts using temporary data and synthetic peers.

The real SDK case tests stdio protocol interoperability. The other cases replace
only the peer transport or model response so authorization and API routing remain
production code; they are not evidence of a paid model or desktop UI acceptance.
"""
import copy
import json
import re
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent import chat, llm
from app.api import routes
from app.integrations import mcp_bridge as bridge, mcp_settings as settings
from app.memory import db


FIXTURE = Path(__file__).parent / 'fixtures/mcp_server.py'
MISSING = object()


def service(index, **overrides):
    return {
        'id': f'service{index}', 'label': f'Synthetic service {index}',
        'enabled': True, 'trusted': True, 'transport': 'stdio',
        'command': sys.executable, 'args': [str(FIXTURE)], 'env_names': [],
        'workspace': 'studypal', 'course': None, 'public_data': False,
        'network': False, 'timeout_seconds': 3,
        'tools': {'lookup': {'read_only': True,
                             'fixed_arguments': {'query': f'service{index}'}}},
        **overrides,
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(db, 'APP_DIR', tmp_path / 'app')
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'app/acceptance.sqlite')
    db.apply_schema()
    conn = db.connect()
    try:
        workspace = conn.execute("SELECT id FROM workspaces WHERE key='studypal'").fetchone()[0]
        conn.executemany('INSERT INTO courses(name,workspace_id) VALUES(?,?)',
                         [('Accounting', workspace), ('Economics', workspace)])
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(chat, 'system_for', lambda key: 'Synthetic acceptance assistant.')
    monkeypatch.setattr(chat.platform_manual, 'should_search', lambda question: False)
    monkeypatch.setattr(routes.ContextCompiler, 'build_context', lambda *args, **kw: None)
    monkeypatch.setattr(llm, 'available', lambda tier='fast': True)

    def unexpected_model(*args, **kwargs):
        pytest.fail('This test must not call the model API')

    monkeypatch.setattr(llm, 'chat_with_tools', unexpected_model)
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 12345)) as test_client:
        yield test_client


def save(client, servers):
    before = client.get('/api/extensions/mcp')
    assert before.status_code == 200, before.text
    response = client.put('/api/extensions/mcp', json={
        'servers': servers, 'revision': before.json()['revision'],
    })
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def peer(monkeypatch):
    calls = []

    async def exchange(server, name=None, arguments=None):
        calls.append((server['id'], name, copy.deepcopy(arguments)))
        if name is not None:
            return {'text': arguments['query'] + ' value', 'server': server['id'], 'tool': name}
        schema = {'type': 'object', 'properties': {'query': {'type': 'string'}},
                  'required': ['query'], 'additionalProperties': False}
        return [{'name': name, 'inputSchema': copy.deepcopy(schema),
                 'annotations': {'readOnlyHint': read_only}}
                for name, read_only in [('lookup', True), ('delete', False), ('unlisted', True)]]

    monkeypatch.setattr(bridge, '_exchange', exchange)
    return calls


def tool_names(toolset):
    return {tool['function']['name'] for tool in toolset.tools}


def model_capture(monkeypatch):
    seen = []

    def model(tier, messages, tools, executor, **kwargs):
        names = {tool['function']['name'] for tool in tools}
        extensions = sorted(name for name in names if name.startswith('mcp_'))
        results = [executor(name, {}) for name in extensions]
        denied = executor('mcp_service_not_selected_lookup', {})
        assert 'error' in denied
        seen.append({'names': names, 'results': results, 'messages': messages, 'tools': tools})
        return 'Synthetic acceptance answer.', [], ''

    monkeypatch.setattr(llm, 'chat_with_tools', model)
    return seen


def test_add_second_third_fourth_preserves_each_service_and_revision_without_execution(client, peer):
    servers = []
    revisions = {client.get('/api/extensions/mcp').json()['revision']}
    for index in range(1, 5):
        servers.append(service(index))
        saved = save(client, servers)
        assert saved['servers'] == servers
        assert saved['revision'] not in revisions
        revisions.add(saved['revision'])
        assert client.get('/api/extensions/mcp').json()['servers'] == servers
        assert json.loads(settings.config_path().read_text()) == {'servers': servers}
    assert peer == [], 'Saving or reading service configuration must not execute a peer'

    edited = copy.deepcopy(servers)
    edited[2]['label'] = 'Edited third service'
    assert save(client, edited)['servers'] == edited
    edited.pop(1)
    assert save(client, edited)['servers'] == edited
    assert peer == []


def test_duplicate_id_after_four_services_is_rejected_without_overwriting(client, peer):
    servers = [service(index) for index in range(1, 5)]
    saved = save(client, servers)
    response = client.put('/api/extensions/mcp', json={
        'revision': saved['revision'], 'servers': servers + [service(4, label='Duplicate')],
    })
    assert response.status_code == 422
    assert client.get('/api/extensions/mcp').json()['servers'] == servers
    assert peer == []


def test_real_sdk_four_same_named_tools_are_distinct_and_each_can_be_tested(client):
    pytest.importorskip('mcp')
    servers = [service(index) for index in range(1, 5)]
    save(client, servers)
    tools = bridge.discover('studypal', web_mode='off')
    assert tools.errors == [], tools.errors
    assert tool_names(tools) == {f'mcp_service{index}_lookup' for index in range(1, 5)}
    for index in range(1, 5):
        response = client.post('/api/extensions/mcp/test', json={
            'id': f'service{index}', 'tool': 'lookup', 'arguments': {},
            'workspace': 'studypal', 'course': None, 'web_mode': 'off',
        })
        assert response.status_code == 200, response.text
        assert response.json()['ok'], response.text
        assert response.json()['result']['text'] == f'service{index} value'


def test_selected_fourth_service_does_not_contact_earlier_peers(client, peer):
    save(client, [service(index) for index in range(1, 5)])
    selected = bridge.discover('studypal', web_mode='off', server_id='service4')
    assert tool_names(selected) == {'mcp_service4_lookup'}
    assert peer == [('service4', None, None)]
    assert selected.call('mcp_service4_lookup', {})['text'] == 'service4 value'
    assert 'error' in selected.call('mcp_service1_lookup', {})
    assert [entry[0] for entry in peer] == ['service4', 'service4']


@pytest.mark.parametrize('override,workspace,course,mode', [
    ({'enabled': False}, 'studypal', None, 'auto'),
    ({'trusted': False}, 'studypal', None, 'auto'),
    ({'workspace': 'workpal', 'public_data': True}, 'studypal', None, 'auto'),
    ({'course': 'Accounting'}, 'studypal', 'Economics', 'auto'),
    ({'course': None}, 'studypal', 'Accounting', 'auto'),
    ({'network': True}, 'studypal', None, 'off'),
])
def test_explicit_service_never_overrides_authorization_or_falls_back(
        client, peer, override, workspace, course, mode):
    save(client, [service(1, public_data=True), service(4, **override)])
    selected = bridge.discover(workspace, course, mode, server_id='service4')
    assert selected.tools == []
    assert peer == [], 'An unauthorized choice must not launch it or an automatic fallback'


def test_public_service_crosses_course_but_not_workspace_and_missing_choice_is_closed(client, peer):
    save(client, [service(1, public_data=True), service(4, public_data=True)])
    assert tool_names(bridge.discover('studypal', 'Accounting', server_id='service4')) == {'mcp_service4_lookup'}
    peer.clear()
    assert bridge.discover('workpal', 'Accounting', server_id='service4').tools == []
    assert bridge.discover('studypal', server_id='deleted_service').tools == []
    assert peer == []


@pytest.mark.parametrize('change', [
    {'enabled': False}, {'trusted': False}, {'tools': {}},
    {'workspace': 'workpal'}, {'course': 'Accounting'}, {'network': True}, None,
])
def test_revoking_fourth_service_invalidates_old_binding_and_preserves_others(client, peer, change):
    servers = [service(index) for index in range(1, 5)]
    save(client, servers)
    old = bridge.discover('studypal', web_mode='off')
    assert len(old.tools) == 4
    if change is None:
        servers.pop()
    else:
        servers[-1].update(change)
    save(client, servers)
    before = len(peer)
    assert 'error' in old.call('mcp_service4_lookup', {})
    assert len(peer) == before, 'Revocation must be checked before contacting the service'
    assert old.call('mcp_service1_lookup', {})['text'] == 'service1 value'
    assert tool_names(bridge.discover('studypal', web_mode='off')) == {
        f'mcp_service{index}_lookup' for index in range(1, 4)}


def test_local_allowlist_and_remote_readonly_and_fixed_arguments_all_constrain_model_tools(client, peer):
    candidate = service(4)
    candidate['tools']['delete'] = {'read_only': True}
    save(client, [candidate])
    selected = bridge.discover('studypal', server_id='service4')
    assert tool_names(selected) == {'mcp_service4_lookup'}
    definition = selected.tools[0]['function']
    assert 'query' not in definition['parameters']['properties']
    before = len(peer)
    for name, args in [
        ('mcp_service4_delete', {}), ('mcp_service4_unlisted', {}),
        ('mcp_service4_lookup', {'query': 'Override fixed scope'}),
        ('mcp_service4_lookup', {'server_id': 'service1'}),
    ]:
        assert 'error' in selected.call(name, args)
    assert len(peer) == before
    assert selected.call('mcp_service4_lookup', {})['text'] == 'service4 value'


@pytest.mark.parametrize('endpoint', ['/api/agent/ask', '/api/agent/ask/stream'])
@pytest.mark.parametrize('selection,expected', [
    (MISSING, {f'mcp_service{index}_lookup' for index in range(1, 5)}),
    (None, {f'mcp_service{index}_lookup' for index in range(1, 5)}),
    ('auto', {f'mcp_service{index}_lookup' for index in range(1, 5)}),
    ('none', set()), ('service4', {'mcp_service4_lookup'}),
])
def test_chat_http_contract_applies_choice_to_actual_model_tool_exposure(
        client, peer, monkeypatch, endpoint, selection, expected):
    save(client, [service(index) for index in range(1, 5)])
    seen = model_capture(monkeypatch)
    body = {'question': 'Find synthetic contract data.', 'web_mode': 'off'}
    if selection is not MISSING:
        body['mcp_server_id'] = selection
    response = client.post(endpoint, json=body)
    assert response.status_code == 200, response.text
    if endpoint.endswith('/stream'):
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        results = [event['data'] for event in events if event.get('type') == 'result']
        assert len(results) == 1, events
        assert results[0]['engine'] == 'model'
    else:
        assert response.json()['engine'] == 'model', response.text
    result = results[0] if endpoint.endswith('/stream') else response.json()
    normalized = 'auto' if selection is MISSING or selection is None else selection
    expected_servers = sorted(name.removeprefix('mcp_').removesuffix('_lookup') for name in expected)
    assert result['mcp_selection'] == {
        'requested': normalized, 'selected': expected_servers,
        'status': 'disabled' if normalized == 'none' else 'available',
    }
    assert len(seen) == 1
    actual = {name for name in seen[0]['names'] if name.startswith('mcp_')}
    assert actual == expected, f'{endpoint} ignored or widened mcp_server_id={selection!r}: {actual}'
    assert {'search_memory', 'query_agenda', 'query_manual'} <= seen[0]['names']
    assert not {'web_search', 'web_open'} & seen[0]['names']
    assert all('error' not in result for result in seen[0]['results'])
    if selection == 'none':
        assert peer == [], 'Disabling MCP must skip discovery, not just hide the returned tools'
    elif selection == 'service4':
        assert {entry[0] for entry in peer} == {'service4'}


@pytest.mark.parametrize('endpoint', ['/api/agent/ask', '/api/agent/ask/stream'])
@pytest.mark.parametrize('selection', [False, 4, [], {}, '', '../service4', 'x' * 65])
def test_invalid_chat_selection_is_rejected_before_discovery_or_model(
        client, peer, monkeypatch, endpoint, selection):
    save(client, [service(1)])
    seen = model_capture(monkeypatch)
    response = client.post(endpoint, json={
        'question': 'Find synthetic contract data.', 'web_mode': 'off',
        'mcp_server_id': selection,
    })
    assert response.status_code in (400, 422), (
        f'{endpoint} silently accepted invalid mcp_server_id={selection!r}: {response.status_code}'
    )
    assert seen == []
    assert peer == []


def test_chat_runtime_accepts_selection_and_does_not_expose_another_service(client, peer, monkeypatch):
    save(client, [service(index) for index in range(1, 5)])
    seen = model_capture(monkeypatch)
    result = chat.answer('Find synthetic contract data.', [], web_mode='off', mcp_server_id='service4')
    assert result['ok'], result
    assert {name for name in seen[0]['names'] if name.startswith('mcp_')} == {'mcp_service4_lookup'}
    assert {entry[0] for entry in peer} == {'service4'}


@pytest.mark.parametrize('endpoint', ['/api/agent/ask', '/api/agent/ask/stream'])
def test_deleted_service_choice_reports_unavailable_without_automatic_fallback(
        client, peer, monkeypatch, endpoint):
    save(client, [service(1)])
    seen = model_capture(monkeypatch)
    response = client.post(endpoint, json={
        'question': 'Find synthetic contract data.', 'web_mode': 'off',
        'mcp_server_id': 'deleted_service',
    })
    assert response.status_code == 200, response.text
    if endpoint.endswith('/stream'):
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        result = next(event['data'] for event in events if event.get('type') == 'result')
        selection = next(event for event in events if event.get('type') == 'mcp_selection')
        assert selection['status'] == 'unavailable'
    else:
        result = response.json()
    assert result['mcp_selection'] == {
        'requested': 'deleted_service', 'selected': [], 'status': 'unavailable',
    }
    assert result['mcp_errors'], 'The UI needs a concrete explanation for the unavailable choice'
    assert not any(name.startswith('mcp_') for name in seen[0]['names'])
    assert peer == []


def test_cached_chat_tool_result_cannot_bypass_a_same_turn_revocation(client, peer, monkeypatch):
    servers = [service(1), service(4)]
    save(client, servers)

    def model(tier, messages, tools, executor, **kwargs):
        assert executor('mcp_service4_lookup', {})['text'] == 'service4 value'
        before = len(peer)
        servers[-1]['trusted'] = False
        save(client, servers)
        repeated = executor('mcp_service4_lookup', {})
        assert 'error' in repeated, 'Revoked tools must not return a cached success'
        assert not repeated.get('cached')
        assert len(peer) == before
        return 'Permission revoked during the synthetic turn.', [], ''

    monkeypatch.setattr(llm, 'chat_with_tools', model)
    result = chat.answer('Find synthetic contract data.', [], web_mode='off', mcp_server_id='service4')
    assert result['ok']
    executed = [event for event in result['trace'] if event.get('type') == 'tool']
    assert [event['status'] for event in executed] == ['done', 'error']
    assert all(event['server_id'] == 'service4' and event['remote_tool'] == 'lookup' for event in executed)


def test_model_tool_envelope_contains_no_transport_paths_or_environment_values(client, peer, monkeypatch):
    monkeypatch.setenv('MCP_CONTRACT_TOKEN', 'synthetic-contract-credential')
    save(client, [service(4, env_names=['MCP_CONTRACT_TOKEN'])])
    seen = model_capture(monkeypatch)
    result = chat.answer('Find synthetic contract data.', [], web_mode='off', mcp_server_id='service4')
    assert result['ok']
    payload = json.dumps({'tools': seen[0]['tools'], 'messages': seen[0]['messages']})
    for private in ['synthetic-contract-credential', 'MCP_CONTRACT_TOKEN', sys.executable, str(FIXTURE)]:
        assert private not in payload


def test_one_failed_peer_keeps_other_services_usable_and_redacts_transport_error(client, peer, monkeypatch):
    save(client, [service(index) for index in range(1, 5)])
    exchange = bridge._exchange

    async def partly_failing(server, name=None, arguments=None):
        if server['id'] == 'service1':
            raise RuntimeError('synthetic-private-provider-detail')
        return await exchange(server, name, arguments)

    monkeypatch.setattr(bridge, '_exchange', partly_failing)
    seen = model_capture(monkeypatch)
    result = chat.answer('Find synthetic contract data.', [], web_mode='off', mcp_server_id='auto')
    assert result['ok']
    assert {name for name in seen[0]['names'] if name.startswith('mcp_')} == {
        f'mcp_service{index}_lookup' for index in range(2, 5)}
    assert result['mcp_selection']['selected'] == ['service2', 'service3', 'service4']
    assert result['mcp_errors']
    assert 'synthetic-private-provider-detail' not in json.dumps(result)


@pytest.mark.parametrize('reverse', [False, True])
def test_cross_service_public_name_collision_preserves_both_tools_and_their_bindings(
        client, peer, monkeypatch, reverse):
    servers = [service(1, id='notes', tools={
        'archive_lookup': {'read_only': True, 'fixed_arguments': {'query': 'first'}}}),
        service(4, id='notes_archive')]
    if reverse:
        servers.reverse()
    save(client, servers)
    exchange = bridge._exchange

    async def colliding_names(server, name=None, arguments=None):
        response = await exchange(server, name, arguments)
        if name is None and server['id'] == 'notes':
            response[0]['name'] = 'archive_lookup'
        if name is None:
            response.append(copy.deepcopy(response[0]))
        return response

    monkeypatch.setattr(bridge, '_exchange', colliding_names)
    discovered = bridge.discover('studypal', web_mode='off')
    bindings = {(binding[0]['id'], binding[1]) for binding in discovered.bindings.values()}
    expected = {('notes', 'archive_lookup'), ('notes_archive', 'lookup')}
    assert bindings == expected
    assert discovered.errors == []
    assert len(discovered.tools) == len(discovered.bindings) == 2
    assert set(discovered.selection['selected']) == {'notes', 'notes_archive'}
    assert 'mcp_notes_archive_lookup' in tool_names(discovered)
    for public_name, (server, remote, *_) in discovered.bindings.items():
        assert re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', public_name)
        result = discovered.call(public_name, {})
        expected_query = 'first' if server['id'] == 'notes' else 'service4'
        assert result == {'text': expected_query + ' value', 'server': server['id'], 'tool': remote}
        assert peer[-1] == (server['id'], remote, {'query': expected_query})

    for server in servers:
        selected = bridge.discover('studypal', web_mode='off', server_id=server['id'])
        assert tool_names(selected) == {'mcp_notes_archive_lookup'}
        result = selected.call('mcp_notes_archive_lookup', {})
        assert result['server'] == server['id']
        response = client.post('/api/extensions/mcp/test', json={
            'id': server['id'], 'tool': next(iter(server['tools'])), 'arguments': {},
            'workspace': 'studypal', 'course': None, 'web_mode': 'off',
        })
        assert response.status_code == 200, response.text
        assert response.json()['ok'], response.text
        assert response.json()['result']['server'] == server['id']


def test_three_way_public_name_collision_preserves_each_distinct_remote_binding(client, peer, monkeypatch):
    identities = [('notes', 'archive_lookup_value'), ('notes_archive', 'lookup_value'),
                  ('notes_archive_lookup', 'value')]
    save(client, [service(index, id=server_id, tools={
        remote: {'read_only': True, 'fixed_arguments': {'query': server_id}}})
        for index, (server_id, remote) in enumerate(identities)])
    exchange = bridge._exchange
    remote_names = dict(identities)

    async def colliding_names(server, name=None, arguments=None):
        response = await exchange(server, name, arguments)
        if name is None:
            response[0]['name'] = remote_names[server['id']]
        return response

    monkeypatch.setattr(bridge, '_exchange', colliding_names)
    discovered = bridge.discover('studypal', web_mode='off')

    assert len(discovered.tools) == 3
    assert discovered.errors == []
    assert {(binding[0]['id'], binding[1]) for binding in discovered.bindings.values()} == set(identities)
    for public_name, (server, remote, *_) in discovered.bindings.items():
        assert re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', public_name)
        assert discovered.call(public_name, {})['text'] == server['id'] + ' value'
        assert peer[-1] == (server['id'], remote, {'query': server['id']})
