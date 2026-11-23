import importlib.util
import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import threading
import gzip
import unittest
from unittest.mock import AsyncMock, patch

from app.integrations import mcp_bridge as bridge
from app.memory import db


class MCPBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.scope = patch.object(db, 'APP_DIR', self.root)
        self.scope.start()
        db_path = patch.object(db, 'DB_PATH', self.root / 'isolated.db')
        db_path.start()
        self.addCleanup(db_path.stop)
        self.server = {
            'id': 'fixture', 'enabled': True, 'trusted': True,
            'command': sys.executable,
            'args': [str(Path(__file__).parent / 'fixtures/mcp_server.py')],
            'workspace': 'studypal', 'network': False, 'public_data': False,
            'timeout_seconds': 2,
            'tools': {'lookup': {'read_only': True, 'description': '查询隔离测试资料'}},
        }

    def tearDown(self):
        self.scope.stop()
        self.tmp.cleanup()

    def configure(self):
        target = self.root / 'extensions/mcp.json'
        target.parent.mkdir(exist_ok=True)
        target.write_text(json.dumps({'servers': [self.server]}))

    def test_no_config_is_noop_and_no_dependency_import(self):
        self.assertEqual(bridge.discover('studypal', None, 'auto').tools, [])
        self.assertEqual(bridge.status()['servers'], [])

    def test_public_validation_defaults_and_disabled_drafts(self):
        original = copy.deepcopy(self.server)
        validated = bridge.validate_servers([self.server])
        self.assertEqual(validated[0]['transport'], 'stdio')
        self.assertEqual(self.server, original)
        self.server.update(enabled=False, tools={})
        self.assertEqual(bridge.validate_servers([self.server])[0]['tools'], {})
        for field, value in [('enabled', 1), ('trusted', 'yes'), ('network', 0),
                             ('timeout_seconds', float('nan')), ('timeout_seconds', True),
                             ('env', {'RAW': 'secret'}), ('unknown', 'value'),
                             ('tools', {'lookup': {'read_only': 'true'}}),
                             ('tools', {'lookup': {'fixed_arguments': []}})]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                bridge.validate_servers([{**self.server, field: value}])

    def test_validation_limits_and_raw_environment_rejection(self):
        servers = [{**self.server, 'id': f's{i}'} for i in range(12)]
        self.assertEqual(len(bridge.validate_servers(servers)), 12)
        with self.assertRaises(ValueError):
            bridge.validate_servers(servers + [{**self.server, 'id': 'extra'}])
        for field, value in [('id', []), ('transport', []), ('args', ['x' * 4097]),
                             ('label', 42), ('workspace', 'x' * 129), ('env_names', ['TOKEN=value']),
                             ('timeout_seconds', float('inf')), ('timeout_seconds', 0),
                             ('timeout_seconds', 16), ('timeout_seconds', -1),
                             ('timeout_seconds', 10 ** 500),
                             ('tools', {'lookup': {'fixed_arguments': {1: 'bad'}}}),
                             ('tools', {'lookup': {'fixed_arguments': {'query': {1: 'bad'}}}}),
                             ('tools', {'lookup': {'description': 42}}),
                             ('tools', {'lookup': {'env': {'TOKEN': 'raw'}}})]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                bridge.validate_servers([{**self.server, field: value}])

    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'optional MCP SDK')
    def test_inspect_keeps_existing_policy_safety_checks(self):
        self.server['tools']['lookup']['fixed_arguments'] = {'query': 42}
        result = bridge.inspect_server(self.server)
        self.assertTrue(result['ok'], result)
        self.assertFalse(next(t for t in result['tools'] if t['name'] == 'lookup')['eligible'])
        self.server['env_names'] = ['MCP_TEST_SECRET']
        self.server['tools']['lookup']['fixed_arguments'] = {'query': 'synthetic-fixed-secret'}
        with patch.dict(os.environ, MCP_TEST_SECRET='synthetic-fixed-secret'):
            result = bridge.inspect_server(self.server)
        self.assertNotIn('synthetic-fixed-secret', str(result))
        self.assertFalse(any(t['eligible'] and t['name'] == 'lookup' for t in result['tools']))

    def test_http_url_and_network_validation(self):
        server = {k: v for k, v in self.server.items() if k not in ('command', 'args')}
        server.update(transport='streamable_http', tools={})
        for url in ['http://127.0.0.1:1234/mcp', 'http://localhost/mcp', 'http://[::1]/mcp']:
            self.assertEqual(bridge.validate_servers([{**server, 'url': url}])[0]['url'], url)
        for url in ['http://example.com/mcp', 'https://example.com/mcp',
                    'http://127.0.0.1@evil.test/mcp', 'http://127.0.0.1/mcp?key=x',
                    'http://127.0.0.1/mcp#token', 'http://127.1/mcp',
                    'http://localhost.evil.test/mcp', 'http://127.0.0.1:0/mcp']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                bridge.validate_servers([{**server, 'url': url}])
        server.update(network=True, url='https://example.com/mcp', bearer_env='MCP_TEST_TOKEN')
        self.assertEqual(bridge.validate_servers([server])[0]['bearer_env'], 'MCP_TEST_TOKEN')
        for value in ['Bearer secret', 'sk-raw-secret', '', 123]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                bridge.validate_servers([{**server, 'bearer_env': value}])
        for url in ['https://192.168.1.1/mcp', 'https://169.254.169.254/mcp', 'https://[fc00::1]/mcp']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                bridge.validate_servers([{**server, 'url': url}])

    def test_http_dns_guard_pins_public_address_and_preserves_tls_identity(self):
        import httpx2
        async def check():
            for host in ['10.0.0.1', '127.0.0.1', '169.254.169.254', '::1']:
                request = httpx2.Request('POST', 'https://example.com/mcp')
                with patch.object(bridge.socket, 'getaddrinfo', return_value=[(None, None, None, None, (host, 443))]):
                    with self.assertRaises(ValueError):
                        await bridge._guard_http_request(request)
            request = httpx2.Request('POST', 'https://example.com/mcp')
            with patch.object(bridge.socket, 'getaddrinfo', return_value=[(None, None, None, None, ('8.8.8.8', 443))]):
                await bridge._guard_http_request(request)
            self.assertEqual(request.url.host, '8.8.8.8')
            self.assertEqual(request.headers['host'], 'example.com')
            self.assertEqual(request.extensions['sni_hostname'], 'example.com')
        asyncio.run(check())

    def test_dns_timeout_has_no_shutdown_wait_or_unbounded_queue(self):
        release = threading.Event()
        workers = []
        slots = threading.BoundedSemaphore(2)
        def blocked(*args, **kwargs):
            workers.append(threading.current_thread())
            release.wait(3)
            return [(None, None, None, None, ('8.8.8.8', 443))]
        try:
            with patch.object(bridge, '_DNS_SLOTS', slots), patch.object(bridge.socket, 'getaddrinfo', side_effect=blocked):
                for _ in range(2):
                    start = time.monotonic()
                    with self.assertRaises(TimeoutError):
                        asyncio.run(bridge._resolve_http_host('example.com', 443, 0.05))
                    self.assertLess(time.monotonic() - start, 0.5)
                for _ in range(8):
                    with self.assertRaises(ValueError):
                        asyncio.run(bridge._resolve_http_host('example.com', 443, 0.05))
                self.assertEqual(len(workers), 2)
                self.assertTrue(all(worker.daemon for worker in workers))
        finally:
            release.set()
            for worker in workers:
                worker.join(1)
        self.assertTrue(slots.acquire(blocking=False))
        self.assertTrue(slots.acquire(blocking=False))
        slots.release()
        slots.release()

    def test_http_response_limit_counts_decoded_bytes_and_all_responses(self):
        import httpx2
        async def check():
            for compressed in (False, True):
                class Body(httpx2.AsyncByteStream):
                    closed = False
                    async def __aiter__(self):
                        raw = b'x' * (bridge.MAX_HTTP_BYTES + 1)
                        wire = gzip.compress(raw) if compressed else raw
                        for i in range(0, len(wire), 16384):
                            yield wire[i:i + 16384]
                    async def aclose(self):
                        self.closed = True
                body = Body()
                async def respond(request):
                    return httpx2.Response(200, headers={'content-encoding': 'gzip'} if compressed else {}, stream=body)
                transport = bridge._bounded_http_transport(httpx2.MockTransport(respond))
                async with httpx2.AsyncClient(transport=transport) as client:
                    with self.assertRaises(ValueError):
                        await client.get('http://127.0.0.1/mcp')
                self.assertTrue(body.closed)
            async def respond(request):
                return httpx2.Response(200, content=b'x' * (bridge.MAX_HTTP_BYTES // 2 + 1))
            async with httpx2.AsyncClient(transport=bridge._bounded_http_transport(httpx2.MockTransport(respond))) as client:
                await client.get('http://127.0.0.1/mcp')
                with self.assertRaises(ValueError):
                    await client.get('http://127.0.0.1/mcp')
        asyncio.run(check())

    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'optional MCP SDK')
    def test_real_http_oversized_json_and_sse_fail_before_sdk_parse(self):
        from fixtures.mcp_http_server import running_server
        for mode in ('oversized_json', 'oversized_sse'):
            with running_server(mode) as peer, patch.dict(os.environ, MCP_TEST_TOKEN=peer.token):
                self.server.update(transport='streamable_http', url=peer.url, bearer_env='MCP_TEST_TOKEN')
                with patch('mcp.client.streamable_http.jsonrpc_message_adapter.validate_json', side_effect=AssertionError('oversized input reached parser')) as parser:
                    result = bridge.inspect_server(self.server)
                self.assertFalse(result['ok'])
                parser.assert_not_called()

    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'optional MCP SDK')
    def test_inspect_disabled_trusted_stdio_without_tool_calls(self):
        self.server.update(enabled=False, tools={})
        result = bridge.inspect_server(self.server)
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(result['tools']), 3)
        lookup = next(t for t in result['tools'] if t['name'] == 'lookup')
        self.assertTrue(lookup['read_only'])
        self.assertTrue(lookup['eligible'])
        self.assertFalse(next(t for t in result['tools'] if t['name'] == 'delete')['eligible'])
        self.configure()
        self.assertEqual(bridge.discover('studypal').tools, [])
        self.server['enabled'] = True
        self.configure()
        self.assertEqual(bridge.discover('studypal').tools, [])
        self.assertEqual(set(lookup), {'name', 'description', 'input_schema', 'read_only', 'eligible', 'reason'})
        with patch.object(bridge, '_exchange', side_effect=AssertionError('must not launch')):
            self.assertFalse(bridge.inspect_server({**self.server, 'trusted': False})['ok'])

    def test_closed_model_arguments_reject_unlisted_extras(self):
        self.configure()
        schema = {'type': 'object', 'properties': {'query': {'type': 'string'}}}
        async def exchange(server, name=None, arguments=None):
            if name:
                return {'text': 'ok'}
            return [{'name': 'lookup', 'inputSchema': schema, 'annotations': {'readOnlyHint': True}}]
        with patch.object(bridge, '_exchange', side_effect=exchange):
            session = bridge.discover('studypal')
            name = session.tools[0]['function']['name']
            self.assertIn('error', session.call(name, {'query': 'ok', 'secret_scope': 'override'}))
            self.assertEqual(session.call(name, {'query': 'ok'})['text'], 'ok')

    def test_rejects_extensions_parent_symlink(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'mcp.json').write_text(json.dumps({'servers': [self.server]}))
        (self.root / 'extensions').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            bridge._servers()

    def test_status_reports_actual_transports(self):
        self.server.update(transport='streamable_http', url='http://localhost/mcp')
        self.configure()
        status = bridge.status()
        self.assertEqual(status['servers'][0]['transport'], 'streamable_http')
        self.assertEqual(status['transports'], ['streamable_http'])

    def test_total_discovery_budget_and_selected_server_filter(self):
        self.configure()
        servers = [{**self.server, 'id': f's{i}'} for i in range(12)]
        clock = [0.0]
        budgets = []
        async def exchange(s, name=None, arguments=None):
            budgets.append((s['id'], s['timeout_seconds']))
            clock[0] += 4.0
            return []
        for s in servers:
            s['timeout_seconds'] = 5
        with patch.object(bridge, '_servers', return_value=servers), \
             patch.object(bridge, '_exchange', side_effect=exchange), \
             patch.object(bridge.time, 'monotonic', side_effect=lambda: clock[0]):
            result = bridge.discover('studypal')
            self.assertEqual([b[1] for b in budgets], [5, 5, 5, 3])
            self.assertEqual(len(result.errors), 8)
            budgets.clear()
            bridge.discover('studypal', server_id='s9')
            self.assertEqual(budgets, [('s9', 5)])

    def test_exposed_tools_cap_stops_other_servers(self):
        self.server['tools'] = {f'tool{i}': {'read_only': True} for i in range(12)}
        self.configure()
        servers = bridge.validate_servers([self.server, {**self.server, 'id': 'second'}])
        seen = []
        async def exchange(s, name=None, arguments=None):
            seen.append(s['id'])
            return [{'name': name, 'inputSchema': {'type': 'object'},
                     'annotations': {'readOnlyHint': True}} for name in s['tools']]
        with patch.object(bridge, '_servers', return_value=servers), \
             patch.object(bridge, '_exchange', side_effect=exchange):
            self.assertEqual(len(bridge.discover('studypal').tools), 12)
        self.assertEqual(seen, ['fixture'])

    def test_http_scope_gates_and_revoked_configuration(self):
        self.server.update(transport='streamable_http', url='https://example.com/mcp', network=True)
        self.configure()
        with patch.object(bridge, '_exchange', side_effect=AssertionError('must not connect')):
            for workspace, course, mode in [('lifepal', None, 'auto'),
                                             ('studypal', 'other', 'auto'), ('studypal', None, 'off')]:
                self.assertEqual(bridge.discover(workspace, course, mode).tools, [])
            self.server.update(enabled=False)
            self.configure()
            self.assertEqual(bridge.discover('studypal').tools, [])

        self.server.update(enabled=True, network=False, url='http://127.0.0.1/mcp')
        self.configure()
        async def exchange(s, name=None, arguments=None):
            self.assertIsNone(name, 'revoked calls must not contact the server')
            return [{'name': 'lookup', 'inputSchema': {'type': 'object'},
                     'annotations': {'readOnlyHint': True}}]
        with patch.object(bridge, '_exchange', side_effect=exchange):
            session = bridge.discover('studypal')
            original = copy.deepcopy(self.server)
            for field, value in [('workspace', 'other'), ('course', 'other'), ('network', True),
                                 ('trusted', False), ('enabled', False), ('tools', {}),
                                 ('url', 'http://localhost/other')]:
                self.server = {**original, field: value}
                self.configure()
                self.assertIn('error', session.call(session.tools[0]['function']['name'], {}))

    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'optional MCP SDK')
    def test_http_probe_timeout_failure_and_auth_rejection(self):
        from fixtures.mcp_http_server import running_server
        with running_server('slow_list') as peer, patch.dict(os.environ, MCP_TEST_TOKEN=peer.token):
            self.server.update(transport='streamable_http', url=peer.url, bearer_env='MCP_TEST_TOKEN',
                               timeout_seconds=0.25)
            start = time.monotonic()
            self.assertFalse(bridge.inspect_server(self.server)['ok'])
            self.assertLess(time.monotonic() - start, 2)
            with patch.dict(os.environ, MCP_TEST_TOKEN='wrong-fixture-token'):
                self.assertFalse(bridge.inspect_server(self.server)['ok'])
        self.assertFalse(bridge.inspect_server(self.server)['ok'])

    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'optional MCP SDK')
    def test_real_http_sdk_inspect_call_and_security(self):
        from fixtures.mcp_http_server import running_server
        with running_server() as peer, patch.dict(os.environ, {
            'MCP_TEST_TOKEN': peer.token, 'HTTP_PROXY': 'http://127.0.0.1:1',
            'HTTPS_PROXY': 'http://127.0.0.1:1', 'ALL_PROXY': 'http://127.0.0.1:1', 'NO_PROXY': '',
        }):
            self.server.update(transport='streamable_http', url=peer.url, bearer_env='MCP_TEST_TOKEN')
            self.configure()
            probe = bridge.inspect_server({**self.server, 'enabled': False})
            self.assertTrue(probe['ok'], probe)
            self.assertEqual(peer.calls, [])
            self.assertTrue(next(t for t in probe['tools'] if t['name'] == 'lookup')['eligible'])
            session = bridge.discover('studypal', web_mode='off')
            self.assertEqual(len(session.tools), 1, session.errors)
            name = session.tools[0]['function']['name']
            self.assertEqual(session.call(name, {'query': 'http'})['text'], 'http value')
            self.assertIn('error', session.call(name, {'query': 'ok', 'extra': 'smuggled'}))
            self.assertEqual(session.call(name, {'query': 'secret'})['text'], '[redacted]')
            self.assertIn('error', session.call(name, {'query': 'error'}))
            self.assertTrue(session.call(name, {'query': 'large'})['truncated'])
            self.server['tools']['lookup']['fixed_arguments'] = {'query': 'http-fixed'}
            self.configure()
            fixed_session = bridge.discover('studypal')
            self.assertEqual(fixed_session.call(name, {})['text'], 'http-fixed value')
            self.assertIn('error', fixed_session.call(name, {'query': 'override'}))
            self.assertIn('error', fixed_session.call(name, {'extra': 'smuggled'}))
            del self.server['tools']['lookup']['fixed_arguments']
            self.configure()
            start = time.monotonic()
            self.assertIn('error', session.call(name, {'query': 'slow'}))
            self.assertLess(time.monotonic() - start, 6)
            self.server['trusted'] = False
            self.configure()
            self.assertIn('error', session.call(name, {'query': 'revoked'}))
            self.assertNotIn(peer.token, str(session.tools) + str(probe))

    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'optional MCP SDK')
    def test_real_http_redirect_malformed_and_missing_token(self):
        from fixtures.mcp_http_server import running_server
        with running_server() as peer, patch.dict(os.environ, MCP_TEST_TOKEN=peer.token):
            self.server.update(transport='streamable_http', url=peer.url, bearer_env='MCP_TEST_TOKEN')
            for path in ('redirect', 'malformed'):
                peer.requests.clear()
                with self.assertLogs(level='DEBUG') as logs:
                    result = bridge.inspect_server({**self.server, 'url': peer.url.replace('/mcp', '/' + path)})
                self.assertFalse(result['ok'])
                self.assertNotIn('/mcp', peer.requests)
                self.assertNotIn(peer.token, str(result) + str(logs.output))
            peer.requests.clear()
            with patch.dict(os.environ, MCP_TEST_TOKEN=''):
                self.assertFalse(bridge.inspect_server(self.server)['ok'])
            self.assertEqual(peer.requests, [])

    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'optional MCP SDK')
    def test_real_http_metadata_secret_and_pagination_bounds(self):
        from fixtures.mcp_http_server import running_server
        for mode in ('schema_leak', 'pages'):
            with running_server(mode) as peer, patch.dict(os.environ, MCP_TEST_TOKEN=peer.token):
                self.server.update(transport='streamable_http', url=peer.url, bearer_env='MCP_TEST_TOKEN')
                result = bridge.inspect_server(self.server)
                self.assertTrue(result['ok'], result)
                self.assertNotIn(peer.token, str(result))
                self.assertEqual(peer.calls, [])
                if mode == 'pages':
                    self.assertEqual(len(result['tools']), 100)
                    self.assertEqual(peer.pages, 3)
                else:
                    self.assertFalse(any(t['eligible'] for t in result['tools']))

    def test_policy_filters_before_starting_process(self):
        self.configure()
        with patch.object(bridge, '_exchange', side_effect=AssertionError('must not launch')):
            self.assertEqual(bridge.discover('lifepal', None, 'auto').tools, [])
            self.assertEqual(bridge.discover('studypal', '会计学', 'auto').tools, [])
            self.server['network'] = True
            self.configure()
            self.assertEqual(bridge.discover('studypal', None, 'off').tools, [])
            self.server['trusted'] = False
            self.configure()
            self.assertEqual(bridge.discover('studypal', None, 'auto').tools, [])

    def test_config_and_status_do_not_disclose_command_or_environment(self):
        self.server['env_names'] = ['TEST_MCP_SECRET']
        self.configure()
        with patch.dict(os.environ, TEST_MCP_SECRET='secret-value'):
            result = json.dumps(bridge.status())
        self.assertNotIn('secret-value', result)
        self.assertNotIn(sys.executable, result)
        self.assertNotIn('TEST_MCP_SECRET', result)

    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'optional MCP SDK')
    def test_real_stdio_discovery_call_validation_and_revocation(self):
        self.configure()
        session = bridge.discover('studypal', None, 'auto')
        self.assertEqual(len(session.tools), 1, session.errors)
        name = session.tools[0]['function']['name']
        self.assertTrue(name.startswith('mcp_'))
        self.assertIn('fixture value', session.call(name, {'query': 'fixture'})['text'])
        self.assertIn('error', session.call(name, {'query': 23}))
        self.assertIn('error', session.call('mcp_unknown', {}))
        self.server['enabled'] = False
        self.configure()
        self.assertIn('error', session.call(name, {'query': 'fixture'}))

    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'optional MCP SDK')
    def test_timeout_output_limit_and_tool_errors(self):
        self.configure()
        session = bridge.discover('studypal', None, 'auto')
        name = session.tools[0]['function']['name']
        result = session.call(name, {'query': 'large'})
        self.assertTrue(result['truncated'])
        self.assertLessEqual(len(result['text']), bridge.MAX_OUTPUT)
        started = time.monotonic()
        result = session.call(name, {'query': 'slow'})
        self.assertIn('error', result)
        self.assertLess(time.monotonic() - started, 10)
        result = session.call(name, {'query': 'error'})
        self.assertIn('error', result)
        self.assertNotIn('fixture-secret', str(result))

    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'optional MCP SDK')
    def test_fixed_arguments_cannot_be_overridden(self):
        self.server['tools']['lookup']['fixed_arguments'] = {'query': 'fixed'}
        self.configure()
        session = bridge.discover('studypal', None, 'auto')
        tool = session.tools[0]['function']
        self.assertNotIn('query', tool['parameters']['properties'])
        self.assertIn('error', session.call(tool['name'], {'query': 'override'}))
        self.assertIn('fixed value', session.call(tool['name'], {})['text'])

    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'optional MCP SDK')
    def test_credentials_in_schema_are_not_exposed_to_model(self):
        self.server['env_names'] = ['MCP_TEST_SECRET', 'MCP_TEST_SCHEMA_LEAK']
        self.configure()
        with patch.dict(os.environ, MCP_TEST_SECRET='fake-credential-schema', MCP_TEST_SCHEMA_LEAK='1'):
            session = bridge.discover('studypal', None, 'auto')
        self.assertEqual(session.tools, [])
        self.assertNotIn('fake-credential-schema', str(session.errors))

    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'optional MCP SDK')
    def test_malformed_protocol_does_not_leak_credentials_in_sdk_logs(self):
        self.server['env_names'] = ['MCP_TEST_SECRET']
        self.configure()
        with patch.dict(os.environ, MCP_TEST_SECRET='fake-credential-stdout'):
            session = bridge.discover('studypal', None, 'auto')
            with self.assertLogs('mcp', level='ERROR') as logs:
                result = session.call(session.tools[0]['function']['name'], {'query': 'malformed'})
        self.assertNotIn('fake-credential-stdout', str(logs.output))
        self.assertNotIn('fake-credential-stdout', str(result))

    def test_chat_routes_extension_and_does_not_fall_through_to_calendar(self):
        from app.agent import chat, llm
        session = bridge.ToolSet('studypal', None, 'auto')
        session.tools = [{'type': 'function', 'function': {
            'name': 'mcp_fixture', 'description': 'test', 'parameters': {'type': 'object'}}}]

        def fake_model(tier, messages, tools, executor, **kw):
            self.assertIn('mcp_fixture', [t['function']['name'] for t in tools])
            self.assertEqual(executor('mcp_fixture', {})['text'], 'fixture')
            return 'test', [], ''

        with patch.object(bridge, 'discover', return_value=session), \
             patch.object(session, 'call', return_value={'text': 'fixture'}), \
             patch.object(llm, 'chat_with_tools', side_effect=fake_model), \
             patch.object(chat.repo, 'query_agenda', side_effect=AssertionError('wrong route')):
            result = chat.answer('test', [], web_mode='off')
        self.assertTrue(result['ok'])
        self.assertEqual(next(event for event in result['trace'] if event['type'] == 'tool')['tool'], 'mcp_fixture')
