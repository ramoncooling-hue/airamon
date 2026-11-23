import json
import io
import multiprocessing
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
from unittest.mock import Mock

import pytest

from app.agent import llm


class Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.body


def call_chat(monkeypatch, **kwargs):
    request_body = {}

    def open_completion(request):
        request_body.update(json.loads(request.data.decode('utf-8')))
        return Response(b'{"choices":[{"message":{"content":"{}"}}]}')

    monkeypatch.setattr(llm.settings_store, 'read_raw', lambda: {
        'fast': {'base_url': 'https://example.test/v1', 'model': 'test-model'}
    })
    monkeypatch.setattr(llm, '_open_completion', open_completion)
    result = llm.chat('fast', [], **kwargs)
    return result, request_body


def test_chat_keeps_provider_default_when_json_mode_is_not_requested(monkeypatch):
    result, body = call_chat(monkeypatch)
    assert result == '{}'
    assert 'response_format' not in body
    assert body == {'model': 'test-model', 'messages': [], 'temperature': .3, 'stream': False}


def test_chat_can_request_json_object_response_format(monkeypatch):
    result, body = call_chat(monkeypatch, response_format={'type': 'json_object'})
    assert result == '{}'
    assert body['response_format'] == {'type': 'json_object'}


def test_unsupported_json_mode_is_not_silently_retried_without_constraint(monkeypatch):
    monkeypatch.setattr(llm.settings_store, 'read_raw', lambda: {
        'fast': {'base_url': 'https://example.test/v1', 'model': 'test-model'}
    })
    opener = Mock(side_effect=urllib.error.HTTPError(
        'https://example.test/v1/chat/completions', 400, 'Bad Request', {},
        io.BytesIO(b'{"error":{"message":"response_format unsupported"}}'),
    ))
    monkeypatch.setattr(llm._OPENER, 'open', opener)
    with pytest.raises(llm.LLMError, match='400') as error:
        llm.chat('fast', [], response_format={'type': 'json_object'})
    assert error.value.code == 'model_error'
    assert error.value.http_status_bucket == 'http_4xx'
    assert opener.call_count == 1


@pytest.fixture
def local_provider(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.server.calls += 1
            try:
                mode = self.server.mode
                if mode == 'headers' or (mode == 'retry' and self.server.calls > 1):
                    time.sleep(3)
                self.send_response(503 if mode in {'retry', 'http5'} else 401 if mode == 'http' else 200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                if mode in {'body', 'retry'}:
                    # Bytes keep arriving: an ordinary socket timeout cannot bound this.
                    for _ in range(100):
                        self.wfile.write(b' ')
                        self.wfile.flush()
                        time.sleep(.05)
                else:
                    self.wfile.write(self.server.body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    server.mode, server.calls = 'ok', 0
    server.body = b'{"choices":[{"message":{"content":"synthetic"}}]}'
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(llm.settings_store, 'read_raw', lambda: {
        'fast': {'base_url': f'http://127.0.0.1:{server.server_port}', 'model': 'synthetic'}})
    yield server
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.mark.parametrize('mode', ['headers', 'body', 'retry'])
def test_total_deadline_bounds_transport_retry_and_trickle_body(local_provider, mode):
    local_provider.mode = mode
    children = {p.pid for p in multiprocessing.active_children()}
    started = time.monotonic()
    with pytest.raises(llm.LLMError) as error:
        llm.chat('fast', [], timeout_seconds=.8)
    assert error.value.code == 'timeout'
    assert time.monotonic() - started < 1.6
    assert {p.pid for p in multiprocessing.active_children()} == children
    if mode == 'retry':
        assert local_provider.calls == 2


@pytest.mark.parametrize('mode,body,code', [
    ('http', b'provider-private-key-url-body', 'http'),
    ('ok', b'provider-private-key-url-body', 'json'),
    ('ok', b'{"choices":[{"message":null}]}', 'invalid_message'),
    ('ok', b'{"choices":[{"message":{"content":23}}]}', 'invalid_message'),
])
def test_bounded_errors_are_safe(local_provider, mode, body, code):
    local_provider.mode, local_provider.body = mode, body
    with pytest.raises(llm.LLMError) as error:
        llm.chat('fast', [], timeout_seconds=3)
    assert error.value.code == code
    assert 'provider-private' not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.http_status_bucket == ('http_4xx' if mode == 'http' else 'http_unknown')


def test_bounded_worker_forwards_only_safe_http_status_bucket(local_provider):
    local_provider.mode, local_provider.body = 'http5', b'provider-private-body'
    with pytest.raises(llm.LLMError) as error:
        llm.chat('fast', [], timeout_seconds=3)
    assert error.value.code == 'http'
    assert error.value.http_status_bucket == 'http_5xx'
    assert 'provider-private-body' not in str(error.value)


@pytest.mark.parametrize('bucket,expected', [
    ('http_4xx', 'http_4xx'), ('http_5xx', 'http_5xx'),
    ('http_unknown', 'http_unknown'), ('provider-secret', 'http_unknown'),
    (['provider-secret'], 'http_unknown'),
])
def test_llm_error_http_status_bucket_is_enum_only(bucket, expected):
    error = llm.LLMError('provider body and URL must stay private', http_status_bucket=bucket)
    assert error.http_status_bucket == expected
    assert 'provider body and URL' not in error.http_status_bucket


def test_bounded_chat_success(local_provider):
    assert llm.chat('fast', [], timeout_seconds=3) == 'synthetic'


@pytest.mark.parametrize('budget', [0, -1, float('inf'), float('nan'), True])
def test_invalid_budget_does_not_touch_configuration(monkeypatch, budget):
    monkeypatch.setattr(llm, '_payload', Mock(side_effect=AssertionError('no config')))
    with pytest.raises(ValueError):
        llm.chat('fast', [], timeout_seconds=budget)


def test_tool_loop_shares_deadline_including_fallback(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(llm.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(llm, '_payload', lambda *a, **kw: ('http://synthetic.test', {}, {}))
    deadlines = []

    def completion(url, body, headers, deadline):
        deadlines.append(deadline)
        now[0] += 2
        return {'choices': [{'message': {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'call', 'function': {'name': 'nonce', 'arguments': '{}'}}]}}]}

    def execute(name, args):
        now[0] += 3
        return {'text': 'synthetic'}

    monkeypatch.setattr(llm, '_bounded_completion', completion)
    fallback = Mock(return_value='done')
    monkeypatch.setattr(llm, 'chat', fallback)
    assert llm.chat_with_tools('fast', [], [], execute, max_rounds=1, timeout_seconds=30)[0] == 'done'
    assert deadlines == [130]
    assert fallback.call_args.kwargs['timeout_seconds'] == 25


def test_tool_elapsed_time_cannot_restart_model_budget(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(llm.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(llm, '_payload', lambda *a, **kw: ('http://synthetic.test', {}, {}))
    completion = Mock(return_value={'choices': [{'message': {'tool_calls': [
        {'id': 'call', 'function': {'name': 'nonce', 'arguments': '{}'}}]}}]})
    monkeypatch.setattr(llm, '_bounded_completion', completion)

    def execute(*args):
        now[0] += 31
        return {}

    with pytest.raises(llm.LLMError) as error:
        llm.chat_with_tools('fast', [], [], execute, timeout_seconds=30)
    assert error.value.code == 'timeout'
    assert completion.call_count == 1


@pytest.mark.parametrize('bounded', [False, True], ids=['ordinary', 'deadline'])
@pytest.mark.parametrize('answer', ['missing', None, '', '   '])
def test_reasoning_only_is_rejected_with_safe_structure_metadata(monkeypatch, bounded, answer):
    secret = 'PRIVATE_REASONING_AND_PROVIDER_BODY'
    message = {'role': 'assistant', 'reasoning_content': secret}
    if answer != 'missing':
        message['content'] = answer
    data = {'choices': [{'message': message, 'finish_reason': 'stop'}], 'private': secret}
    monkeypatch.setattr(llm, '_payload', lambda *a, **kw: ('https://example.test', {}, {}))
    ordinary = Mock(return_value=Response(json.dumps(data).encode()))
    transport = Mock(return_value=data)
    monkeypatch.setattr(llm, '_open_completion', ordinary)
    monkeypatch.setattr(llm, '_bounded_completion', transport)

    with pytest.raises(llm.LLMError) as error:
        llm.chat('fast', [], timeout_seconds=3 if bounded else None)

    assert error.value.code == 'invalid_message'
    assert str(error.value) == '模型服务未返回最终答案，请检查模型接口或更换模型后重试。'
    assert error.value.__cause__ is None
    metadata = error.value.response_metadata
    assert metadata['content_present'] is (answer != 'missing')
    assert metadata['content_type'] == ('str' if isinstance(answer, str) and answer != 'missing' else 'null')
    assert metadata['reasoning_content_type'] == 'str'
    assert metadata['reasoning_content_length'] == len(secret)
    assert metadata['tool_calls_present'] is False
    assert metadata['finish_reason'] == 'stop'
    assert secret not in str(error.value) + json.dumps(metadata)
    assert ordinary.call_count + transport.call_count == 1


@pytest.mark.parametrize('bounded', [False, True], ids=['ordinary', 'deadline'])
def test_answer_content_is_preserved_and_reasoning_never_substituted(monkeypatch, bounded):
    data = {'choices': [{'message': {'content': '  final answer  ', 'reasoning_content': 'PRIVATE'}}]}
    monkeypatch.setattr(llm, '_payload', lambda *a, **kw: ('https://example.test', {}, {}))
    monkeypatch.setattr(llm, '_open_completion', lambda *a, **kw: Response(json.dumps(data).encode()))
    monkeypatch.setattr(llm, '_bounded_completion', lambda *a, **kw: data)
    assert llm.chat('fast', [], timeout_seconds=3 if bounded else None) == '  final answer  '


@pytest.mark.parametrize('data', [None, [], {}, {'choices': []}, {'choices': [{'message': None}]}])
def test_invalid_envelopes_have_safe_metadata(data):
    with pytest.raises(llm.LLMError) as error:
        llm._message(data)
    assert error.value.code == 'invalid_message'
    assert error.value.response_metadata['response_type'] in {'null', 'list', 'dict'}


def test_message_metadata_finish_reason_and_fields_cannot_leak_provider_values():
    secret = 'PRIVATE_PROVIDER_MESSAGE'
    data = {'choices': [{'finish_reason': secret, 'message': {
        'content': {'secret': secret}, 'reasoning_content': secret, secret: secret,
    }}], secret: secret}
    with pytest.raises(llm.LLMError) as error:
        llm._message(data)
    assert error.value.response_metadata['finish_reason'] == 'unknown'
    assert error.value.response_metadata['content_type'] == 'dict'
    assert secret not in str(error.value) + json.dumps(error.value.response_metadata)


def test_response_metadata_is_whitelisted_and_bounded():
    error = llm.LLMError('safe', code='invalid_message', response_metadata={
        'response_type': 'dict', 'content_present': False, 'content_type': 'PRIVATE',
        'content_length': -1, 'reasoning_content_length': True, 'tool_calls_length': 2 ** 80,
        'finish_reason': ['PRIVATE'], 'private_field': 'PRIVATE',
    })
    assert error.response_metadata == {'response_type': 'dict', 'content_present': False}


@pytest.mark.parametrize('answer', ['missing', None, '', '   '])
@pytest.mark.parametrize('with_reasoning', [False, True])
def test_tool_call_without_content_is_valid_message_but_not_final_answer(answer, with_reasoning):
    secret = 'PRIVATE_REASONING_SECRET'
    message = {'role': 'assistant', 'tool_calls': [{
        'id': 'call_1', 'type': 'function',
        'function': {'name': 'synthetic', 'arguments': '{}'},
    }]}
    if answer != 'missing':
        message['content'] = answer
    if with_reasoning:
        message['reasoning_content'] = secret
    data = {'choices': [{'message': message}]}

    assert llm._message(data) is message
    with pytest.raises(llm.LLMError) as error:
        llm._answer(data)
    assert error.value.code == 'invalid_message'
    assert str(error.value) == '模型服务未返回最终答案，请检查模型接口或更换模型后重试。'
    assert secret not in str(error.value) + json.dumps(error.value.response_metadata)
