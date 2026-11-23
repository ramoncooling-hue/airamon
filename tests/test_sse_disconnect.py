"""B1.1/P2-5: 端点级 SSE 断连必须取消 worker，而不是继续跑完。

真实 uvicorn + httpx 流式连接：收到首个事件后关闭连接（等价于浏览器关页），
断言：取消标记传到 chat.answer、取消后不再产生事件、worker 线程 5s 内退出、
服务无 500/异常泄漏。
"""

from __future__ import annotations

import json
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from app.api import routes
from app.memory import db

STREAM_PATH = '/api/agent/ask/stream'


def _stub_answer(**overrides):
    data = {
        'ok': True, 'text': 'ok', 'error': '', 'hits': [], 'agenda': None,
        'manual_results': [], 'web_results': [], 'web_error': '', 'web_query': '',
        'trace': [], 'mcp_errors': [],
        'mcp_selection': {'requested': 'auto', 'selected': [], 'status': 'unavailable'},
    }
    data.update(overrides)
    return data


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'APP_DIR', tmp_path / 'app')
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'app' / 'test.db')
    db.apply_schema()
    monkeypatch.setattr(routes.ContextCompiler, 'build_context', lambda *args, **kwargs: None)
    monkeypatch.setattr(routes.llm, 'available', lambda tier='fast': True)
    return tmp_path


@pytest.fixture
def ask_server(isolated_db, monkeypatch):
    """本机真实 uvicorn；fake answer 可观察取消标志并记录 emit 结果。"""
    observed = {}

    def fake_answer(question, hits, role_key, tier, scope, history, **kwargs):
        should_cancel = kwargs['should_cancel']
        emit = kwargs.get('progress')
        observed['started'] = True
        if emit is not None:
            observed['first_emit'] = emit({'type': 'progress', 'message': 'first'})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if should_cancel():
                observed['cancelled'] = True
                break
            time.sleep(0.02)
        else:
            observed['loop_deadline'] = True
        if emit is not None:
            # 取消后生产端不应再接受事件（排队事件不再增长）。
            observed['post_cancel_emit'] = emit({'type': 'progress', 'message': 'late'})
        return _stub_answer()

    monkeypatch.setattr(routes.agent_chat, 'answer', fake_answer)
    app = FastAPI()
    app.include_router(routes.router)
    config = uvicorn.Config(app, host='127.0.0.1', port=0,
                            log_level='error', timeout_keep_alive=1)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True,
                              name='mindock-sse-test-server')
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not server.started:
        time.sleep(0.02)
    assert server.started, 'uvicorn test server did not start'
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield 'http://127.0.0.1:%d' % port, observed
    finally:
        server.should_exit = True
        thread.join(10)
        assert not thread.is_alive(), 'uvicorn test server did not stop'


def test_client_disconnect_cancels_worker_and_stops_events(ask_server):
    base_url, observed = ask_server
    stream_url = base_url + STREAM_PATH
    with httpx.Client(timeout=10.0) as client:
        with client.stream('POST', stream_url,
                           json={'question': '断连测试', 'web_mode': 'off'}) as response:
            assert response.status_code == 200
            first = None
            for line in response.iter_lines():
                if line.startswith('data: '):
                    first = json.loads(line[len('data: '):])
                    break
            assert first is not None and first.get('type') == 'progress'
        # 退出 with = 关闭连接（浏览器关页），stream() 的 finally 应置 cancel_event。

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not observed.get('cancelled'):
        time.sleep(0.02)
    assert observed.get('cancelled') is True, observed

    # 取消后 emit 返回 False：不再为已断开的客户端产生事件。
    assert observed.get('post_cancel_emit') is False, observed

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not any(item.name == 'mindock-ask-stream' for item in threading.enumerate()):
            break
        time.sleep(0.02)
    assert not any(item.name == 'mindock-ask-stream' for item in threading.enumerate()), \
        'ask worker thread leaked after client disconnect'

    # 无 500/异常泄漏：同一服务仍能正常处理请求（空问题走 400 校验）。
    with httpx.Client(timeout=5.0) as client:
        probe = client.post(stream_url, json={'question': '   ', 'web_mode': 'off'})
    assert probe.status_code == 400
