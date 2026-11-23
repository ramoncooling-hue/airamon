"""Verify a candidate macOS frozen backend's MCP API using synthetic fixtures.

Run with the development interpreter that has the official MCP SDK installed.
The API host is always the app's Mach-O executable, never a source backend.
No models or personal settings are read. Reports never include HTTP bodies,
tokens, fixture addresses, process output or exception strings.
"""
import argparse
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
API = '/api/extensions/mcp'
FAILURES = {'assertion_failed', 'candidate_missing', 'candidate_not_macho',
            'response_limit', 'transport_error', 'invalid_json', 'backend_exited',
            'startup_timeout', 'process_timeout', 'verification_error'}


class VerificationError(RuntimeError):
    pass


def require(condition):
    if not condition:
        raise VerificationError('assertion_failed')


def backend_path(app):
    executable = Path(app).resolve() / 'Contents/Resources/backend/mindock-backend'
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise VerificationError('candidate_missing')
    with executable.open('rb') as source:
        magic = source.read(4)
    if magic not in {b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe',
                     b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca',
                     b'\xca\xfe\xba\xbf', b'\xbf\xba\xfe\xca'}:
        raise VerificationError('candidate_not_macho')
    return executable


def environment(data, token, fixture_token):
    # Do not inherit model credentials, Python paths, locks or desktop session
    # settings from the shell. Both app data and home are disposable.
    return {'HOME': str(data), 'TMPDIR': str(data), 'LANG': 'en_US.UTF-8',
            'PATH': '/usr/bin:/bin:/usr/sbin:/sbin',
            'CYBERPAL_DATA_DIR': str(data),
            'MINDOCK_SESSION_FILE': str(data / 'session.json'),
            'MINDOCK_SESSION_TOKEN': token,
            'MINDOCK_PARENT_PID': str(os.getpid()),
            'MINDOCK_SHUTDOWN_FILE': str(data / 'shutdown'),
            'MCP_DESKTOP_FIXTURE_TOKEN': fixture_token}


class Backend:
    def __init__(self, executable, data, env, log):
        self.executable, self.data, self.env, self.log = executable, data, env, log
        self.process = None
        self.base = ''
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, path, body=None, method='GET', *, origin=None, authenticated=True):
        headers = {'Content-Type': 'application/json', 'Origin': origin or self.base}
        if authenticated:
            headers['X-MinDock-Session'] = self.env['MINDOCK_SESSION_TOKEN']
        request = urllib.request.Request(self.base + path, method=method, headers=headers,
            data=json.dumps(body).encode() if body is not None else None)
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read(256 * 1024 + 1)
                if len(raw) > 256 * 1024:
                    raise VerificationError('response_limit')
                return response.status, json.loads(raw)
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            return code, {}
        except urllib.error.URLError:
            raise VerificationError('transport_error') from None
        except (ValueError, UnicodeError):
            raise VerificationError('invalid_json') from None

    def ok(self, path, body=None, method='GET'):
        status, result = self.request(path, body, method)
        require(status == 200 and isinstance(result, dict))
        return result

    def start(self):
        descriptor = self.data / 'session.json'
        descriptor.unlink(missing_ok=True)
        (self.data / 'shutdown').unlink(missing_ok=True)
        self.process = subprocess.Popen([str(self.executable)], env=self.env,
            cwd=str(self.data), stdout=self.log, stderr=subprocess.STDOUT,
            start_new_session=True)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise VerificationError('backend_exited')
            if descriptor.exists():
                try:
                    port = json.loads(descriptor.read_text())['port']
                    require(isinstance(port, int) and 1024 <= port <= 65535)
                    self.base = 'http://127.0.0.1:' + str(port)
                    if self.request('/api/health')[0] == 200:
                        return
                except (OSError, ValueError, KeyError, VerificationError):
                    pass
            time.sleep(.2)
        raise VerificationError('startup_timeout')

    def stop(self):
        process = self.process
        if process is None:
            return
        forced = False
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    forced = True
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
            require(not forced and process.returncode in (0, -signal.SIGTERM))
        finally:
            # Also reap orphan fixture processes if the backend failed mid-call.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process = None


def fixtures(http_url):
    common = {'enabled': True, 'trusted': True, 'workspace': 'studypal',
              'course': None, 'public_data': False, 'network': False,
              'timeout_seconds': 5}
    return [
        {**common, 'id': 'desktop_stdio', 'transport': 'stdio',
         'command': sys.executable, 'args': [str(ROOT / 'tests/fixtures/mcp_model_server.py')],
         'env_names': [], 'tools': {'nonce': {'read_only': True}}},
        {**common, 'id': 'desktop_http', 'transport': 'streamable_http',
         'url': http_url, 'bearer_env': 'MCP_DESKTOP_FIXTURE_TOKEN',
         'tools': {'lookup': {'read_only': True}}},
    ]


def exercise(backend, servers, peer, record):
    initial = backend.ok(API)
    require(initial['servers'] == [] and initial['sdk_installed'] is True)
    record('get_empty')
    for server in servers:
        result = backend.ok(API + '/probe', {'server': {**server, 'enabled': False}}, 'POST')
        name = next(iter(server['tools']))
        require(result.get('ok') is True and any(
            tool['name'] == name and tool['eligible'] for tool in result['tools']))
        require(backend.ok(API)['revision'] == initial['revision'])
        record('probe_' + server['transport'])
    require(peer.calls == [])
    saved = backend.ok(API, {'servers': servers, 'revision': initial['revision']}, 'PUT')
    require(saved['servers'] == servers)
    require(backend.ok(API)['servers'] == servers)
    require(backend.request(API, {'servers': [], 'revision': initial['revision']}, 'PUT')[0] == 409)
    record('put_and_reload')
    for path, method, body in [
        (API, 'GET', None), (API, 'PUT', {'servers': [], 'revision': saved['revision']}),
        (API + '/probe', 'POST', {'server': servers[0]}),
        (API + '/test', 'POST', {'id': 'desktop_stdio', 'tool': 'nonce',
          'arguments': {}, 'workspace': 'studypal', 'web_mode': 'off'}),
    ]:
        require(backend.request(path, body, method, origin='https://foreign.invalid')[0] == 403)
    require(backend.request(API, authenticated=False)[0] == 403)
    require(backend.ok(API)['revision'] == saved['revision'] and peer.calls == [])
    record('cross_origin_and_session_denied')

    def call(server):
        name = next(iter(server['tools']))
        query = 'synthetic_' + secrets.token_hex(12)
        body = {'id': server['id'], 'tool': name, 'arguments': {} if name == 'nonce' else {'query': query},
                'workspace': 'studypal', 'course': None, 'web_mode': 'off'}
        result = backend.ok(API + '/test', body, 'POST')
        require(result.get('ok') is True)
        text = result['result'].get('text', '')
        require(bool(re.fullmatch(r'MCP_SYNTHETIC_[0-9a-f]{48}', text)) if name == 'nonce'
                else text == query + ' value')
        wrong_scope = backend.ok(API + '/test', {**body, 'workspace': 'lifepal'}, 'POST')
        require(wrong_scope.get('ok') is False)

    for server in servers:
        call(server)
        record('test_' + server['transport'])
    require(peer.calls == ['lookup'])
    backend.stop()
    record('stop_before_restart')
    backend.start()
    require(backend.ok(API)['revision'] == saved['revision'])
    require(backend.ok(API)['servers'] == servers)
    for server in servers:
        call(server)
    require(peer.calls == ['lookup', 'lookup'])
    record('restart_reload_and_real_calls')
    # The management API deletes by saving the revised list, not HTTP DELETE.
    empty = backend.ok(API, {'servers': [], 'revision': saved['revision']}, 'PUT')
    require(empty['servers'] == [] and backend.ok(API)['servers'] == [])
    require(backend.request(API + '/test', {'id': 'desktop_stdio', 'tool': 'nonce',
        'arguments': {}, 'workspace': 'studypal', 'web_mode': 'off'}, 'POST')[0] == 422)
    record('delete_via_put_and_verify_revocation')
    backend.stop()
    backend.start()
    require(backend.ok(API)['servers'] == [])
    record('deletion_survives_restart')


def exercise_models(backend, record):
    api = '/api/settings/models'
    initial = backend.ok(api)
    require(initial['profiles'] == [])
    secret = 'synthetic-' + secrets.token_hex(24)
    profiles = [{'id': 'model' + str(i), 'name': 'Synthetic ' + str(i),
                 'provider': 'openai-compatible', 'base_url': 'https://model.invalid/v1',
                 'model': 'synthetic', 'api_key': secret} for i in range(4)]
    routes = {'fast': 'model3', 'reasoning': 'model1'}
    saved = backend.ok(api, {'profiles': profiles, 'routes': routes,
                            'revision': initial['revision']}, 'PUT')
    require(len(saved['profiles']) == 4 and saved['routes'] == routes)
    require(secret not in json.dumps(saved) and all(p['api_key_set'] for p in saved['profiles']))
    require(backend.request(api, authenticated=False)[0] == 403)
    require(backend.request(api, origin='https://foreign.invalid')[0] == 403)
    require(backend.request(api, {**saved, 'revision': initial['revision']}, 'PUT')[0] == 409)
    record('four_model_profiles_and_access_guards')
    edited = {**saved, 'profiles': [{**p, 'name': p['name'] + ' edited', 'api_key': ''}
                                  for p in saved['profiles']]}
    saved = backend.ok(api, edited, 'PUT')
    require(all(p['api_key_set'] for p in saved['profiles']))
    require(backend.request(api, {**saved, 'profiles': saved['profiles'][:3]}, 'PUT')[0] == 422)
    require(backend.ok(api) == saved)
    record('model_secret_retention_and_referenced_delete_denied')
    backend.stop()
    backend.start()
    require(backend.ok(api) == saved and secret not in json.dumps(backend.ok(api)))
    record('model_profiles_restart_and_masking')
    cleared = {**saved, 'profiles': [{**p, 'clear_api_key': True} for p in saved['profiles']]}
    saved = backend.ok(api, cleared, 'PUT')
    require(all(not p['api_key_set'] for p in saved['profiles']))
    saved = backend.ok(api, {**saved, 'profiles': [], 'routes': {'fast': '', 'reasoning': ''}}, 'PUT')
    require(saved['profiles'] == [])
    record('model_explicit_secret_clear_and_delete')


def verify(app):
    started = time.monotonic()
    report = {'schema_version': 1, 'run_id': uuid.uuid4().hex, 'ok': False,
              'layer': 'frozen_backend_actual_mcp_api', 'synthetic_only': True,
              'personal_data_access': False, 'model_calls': False,
              'external_python': 'fixtures_only', 'checks': [], 'failure_code': ''}
    phase = 'candidate'

    def record(name):
        nonlocal phase
        report['checks'].append({'check': name, 'status': 'passed',
                                'elapsed_seconds': round(time.monotonic() - started, 3)})
        phase = name

    try:
        executable = backend_path(app)
        with executable.open('rb') as source:
            report['backend_sha256'] = hashlib.file_digest(source, 'sha256').hexdigest()
        phase = 'fixture_startup'
        spec = importlib.util.spec_from_file_location('desktop_http_fixture', ROOT / 'tests/fixtures/mcp_http_server.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix='mindock-mcp-desktop-') as directory:
            data = Path(directory).resolve()
            with (data / 'private-test.log').open('w') as log, redirect_stdout(log), redirect_stderr(log):
                token, fixture_token = secrets.token_hex(32), secrets.token_hex(32)
                with module.running_server(token=fixture_token) as peer:
                    backend = Backend(executable, data, environment(data, token, fixture_token), log)
                    try:
                        phase = 'backend_startup'
                        backend.start()
                        require(Path(backend.ok('/api/runtime')['data_dir']).resolve() == data)
                        record('isolated_frozen_startup')
                        phase = 'mcp_api'
                        exercise(backend, fixtures(peer.url), peer, record)
                        phase = 'model_api'
                        exercise_models(backend, record)
                    finally:
                        backend.stop()
                    record('graceful_shutdown')
                record('http_fixture_shutdown')
        report['ok'] = True
    except VerificationError as exc:
        report['failure_code'] = str(exc) if str(exc) in FAILURES else 'verification_error'
    except subprocess.TimeoutExpired:
        report['failure_code'] = 'process_timeout'
    except Exception:
        report['failure_code'] = 'verification_error'
    report['last_completed_check'] = report['checks'][-1]['check'] if report['checks'] else None
    report['phase'] = phase
    report['elapsed_seconds'] = round(time.monotonic() - started, 3)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('app', type=Path, help='Candidate MinDock.app path')
    args = parser.parse_args(argv)
    report = verify(args.app)
    print(json.dumps(report, indent=2))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
