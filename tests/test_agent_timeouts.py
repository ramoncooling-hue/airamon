"""B1 · 单轮问答预算、协作式取消与流式收尾。

全部模型调用都走 loopback 合成提供商或直接替换函数，不访问真实模型。
"""
import json
import multiprocessing
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.agent import chat, llm
from app.api import routes
from app.memory import db


def _stub_answer(**overrides):
    data = {
        'ok': True, 'text': 'ok', 'error': '', 'hits': [], 'agenda': None,
        'manual_results': [], 'web_results': [], 'web_error': '', 'web_query': '',
        'trace': [],
    }
    data.update(overrides)
    return data


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'APP_DIR', tmp_path / 'app')
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'app' / 'test.db')
    db.apply_schema()
    monkeypatch.setattr(routes.ContextCompiler, 'build_context', lambda *args, **kwargs: None)
    monkeypatch.setattr(routes.llm, 'available', lambda tier='fast': True)


def test_ask_budget_defaults_and_env_override(monkeypatch):
    monkeypatch.delenv('MINDOCK_ASK_BUDGET_SECONDS', raising=False)
    assert routes.ASK_BUDGET_SECONDS == 240.0
    assert routes._ask_budget_seconds() == 240.0
    monkeypatch.setenv('MINDOCK_ASK_BUDGET_SECONDS', '30')
    assert routes._ask_budget_seconds() == 30.0
    for invalid in ('abc', '', '0', '-5', 'NaN'):
        monkeypatch.setenv('MINDOCK_ASK_BUDGET_SECONDS', invalid)
        assert routes._ask_budget_seconds() == routes.ASK_BUDGET_SECONDS


def test_non_stream_ask_passes_budget_and_no_cancel_hook(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    captured = {}

    def fake_answer(*args, **kwargs):
        captured.update(kwargs)
        return _stub_answer()

    monkeypatch.setattr(routes.agent_chat, 'answer', fake_answer)
    result = routes._agent_ask(routes.AskIn(question='预算测试', web_mode='off'))
    assert 0 < captured['timeout_seconds'] <= routes.ASK_BUDGET_SECONDS
    assert captured['should_cancel'] is None
    assert result['persisted'] is False
    assert result['conversation_id'] is None
    assert result['error_code'] == ''


def test_small_budget_is_forwarded(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv('MINDOCK_ASK_BUDGET_SECONDS', '12')
    captured = {}

    def fake_answer(*args, **kwargs):
        captured.update(kwargs)
        return _stub_answer()

    monkeypatch.setattr(routes.agent_chat, 'answer', fake_answer)
    routes._agent_ask(routes.AskIn(question='短预算', web_mode='off'))
    assert 0 < captured['timeout_seconds'] <= 12


def test_worker_cancel_event_reaches_answer(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    cancel_event = threading.Event()
    seen = {}

    def fake_answer(*args, **kwargs):
        should_cancel = kwargs['should_cancel']
        seen['before'] = should_cancel()
        cancel_event.set()
        seen['after'] = should_cancel()
        return _stub_answer()

    monkeypatch.setattr(routes.agent_chat, 'answer', fake_answer)
    routes._ASK_CANCEL.event = cancel_event
    try:
        routes._agent_ask(routes.AskIn(question='取消信号', web_mode='off'))
    finally:
        routes._ASK_CANCEL.event = None
    assert seen == {'before': False, 'after': True}


def test_llm_cancel_before_request_raises_cancelled_without_provider():
    with pytest.raises(llm.LLMError) as error:
        llm.chat('fast', [], should_cancel=lambda: True)
    assert error.value.code == 'cancelled'
    with pytest.raises(llm.LLMError) as error:
        llm.chat_with_tools('fast', [], [], lambda name, args: {}, should_cancel=lambda: True)
    assert error.value.code == 'cancelled'
    assert 'cancelled' in llm.SAFE_ERROR_CODES


@pytest.fixture
def trickle_provider(monkeypatch):
    """一个持续滴流、不结束的本地提供商。"""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            try:
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                for _ in range(200):
                    self.wfile.write(b' ')
                    self.wfile.flush()
                    time.sleep(.02)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(llm.settings_store, 'read_raw', lambda: {
        'fast': {'base_url': f'http://127.0.0.1:{server.server_port}', 'model': 'synthetic'}})
    yield server
    server.shutdown()
    server.server_close()
    thread.join()


def test_llm_cancel_mid_trickle_releases_killable_transport(trickle_provider):
    children = {process.pid for process in multiprocessing.active_children()}
    cancel_event = threading.Event()
    timer = threading.Timer(.3, cancel_event.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(llm.LLMError) as error:
            llm.chat('fast', [], timeout_seconds=10, should_cancel=cancel_event.is_set)
    finally:
        timer.cancel()
    assert error.value.code == 'cancelled'
    assert time.monotonic() - started < 3
    time.sleep(.2)
    assert {process.pid for process in multiprocessing.active_children()} == children


@pytest.mark.parametrize('code', ['timeout', 'cancelled'])
def test_answer_reports_machine_readable_error_code(monkeypatch, code):
    def failing(*args, **kwargs):
        raise llm.LLMError('模型未完成本轮回答', code=code)

    monkeypatch.setattr(llm, 'chat_with_tools', failing)
    result = chat.answer('测试问题', [], web_mode='off')
    assert result['ok'] is False
    assert result['error']
    assert result['error_code'] == code


def test_agent_ask_returns_envelope_for_model_timeout(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)

    def timeout(*args, **kwargs):
        raise llm.LLMError('本轮问答超过预算', code='timeout')

    monkeypatch.setattr(routes.agent_chat, 'answer', timeout)
    result = routes._agent_ask(routes.AskIn(question='超时测试', web_mode='off'))
    assert result['engine'] == 'local'
    assert result['error']
    assert result['error_code'] == 'timeout'
    assert result['persisted'] is False
    assert result['hits'] == [] and result['trace'] == []
    assert result['mcp_selection']['status'] == 'unavailable'


def test_agent_ask_envelope_preserves_error_message_for_ui(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)

    def timeout(*args, **kwargs):
        raise llm.LLMError('upstream said: sk-private-value', code='http')

    monkeypatch.setattr(routes.agent_chat, 'answer', timeout)
    result = routes._agent_ask(routes.AskIn(question='脱敏测试', web_mode='off'))
    assert result['error_code'] == 'http'
    assert 'sk-private-value' in result['error']


def test_stream_endpoint_finishes_after_result_event(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(routes.agent_chat, 'answer', lambda *args, **kwargs: _stub_answer())
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        response = client.post('/api/agent/ask/stream', json={'question': '流式', 'web_mode': 'off'})
    assert response.status_code == 200
    events = [line[6:] for line in response.text.splitlines() if line.startswith('data: ')]
    payloads = [json.loads(event) for event in events]
    result = next(item['data'] for item in payloads if item.get('type') == 'result')
    assert result['persisted'] is False
    assert result['conversation_id'] is None
    assert result['text'] == 'ok'
