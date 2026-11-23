"""B1 · 显式 conversation_id 时由服务端持久化会话。

未带 conversation_id 时保持旧客户端语义：不建会话、不落库。
"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes
from app.memory import chat_repo, db


ANSWER = {
    'ok': True, 'text': '服务器回答正文', 'error': '',
    'hits': [{'title': '资料', 'citation': '记忆1'}],
    'agenda': None, 'manual_results': [], 'web_results': [], 'web_error': '',
    'web_query': '', 'trace': [{'type': 'tool', 'tool': 'search_memory', 'status': 'done'}],
    'mcp_errors': [],
    'mcp_selection': {'requested': 'auto', 'selected': [], 'status': 'unavailable'},
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'APP_DIR', tmp_path / 'app')
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'app' / 'test.db')
    db.apply_schema()
    monkeypatch.setattr(routes.ContextCompiler, 'build_context', lambda *args, **kwargs: None)
    monkeypatch.setattr(routes.llm, 'available', lambda tier='fast': True)
    calls = []

    def fake_answer(*args, **kwargs):
        calls.append({'args': args, 'kwargs': kwargs})
        return dict(ANSWER)

    monkeypatch.setattr(routes.agent_chat, 'answer', fake_answer)
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        yield {'calls': calls, 'client': client}


def _conversation(**overrides):
    return chat_repo.create_conversation(**overrides)


def test_ask_persists_user_and_assistant_when_conversation_given(env):
    conversation = _conversation()
    result = routes._agent_ask(routes.AskIn(
        question='项目代号蓝鲸', web_mode='off', conversation_id=conversation['id']))
    assert result['persisted'] is True
    assert result['conversation_id'] == conversation['id']
    saved = chat_repo.get_conversation(conversation['id'])
    assert [message['role'] for message in saved['messages']] == ['user', 'assistant']
    assert saved['messages'][0]['content'] == '项目代号蓝鲸'
    assert saved['messages'][1]['content'] == '服务器回答正文'
    assert saved['messages'][1]['engine'] == 'model'
    assert saved['messages'][1]['steps'] == ANSWER['trace']
    assert saved['title'] == '项目代号蓝鲸'


def test_ask_without_conversation_id_does_not_create_or_persist(env):
    before = len(chat_repo.list_conversations())
    history = [{'role': 'user', 'content': '旧历史'}]
    result = routes._agent_ask(routes.AskIn(
        question='不落库', web_mode='off', history=history))
    assert result['persisted'] is False
    assert result['conversation_id'] is None
    assert len(chat_repo.list_conversations()) == before
    assert env['calls'][-1]['args'][5] == history


def test_unknown_conversation_id_is_rejected(env):
    with pytest.raises(routes.HTTPException) as error:
        routes._agent_ask(routes.AskIn(
            question='未知会话', web_mode='off', conversation_id=99999))
    assert error.value.status_code == 400
    assert env['calls'] == []


def test_server_side_history_is_used_when_client_sends_none(env):
    conversation = _conversation()
    for index in range(8):
        chat_repo.add_message(conversation['id'],
                              'user' if index % 2 == 0 else 'assistant',
                              f'旧消息{index}')
    routes._agent_ask(routes.AskIn(
        question='接着聊', web_mode='off', conversation_id=conversation['id']))
    history = env['calls'][-1]['args'][5]
    assert len(history) == 8
    assert history[0] == {'role': 'user', 'content': '旧消息0'}
    assert history[-1] == {'role': 'assistant', 'content': '旧消息7'}


def test_server_side_history_is_bounded_to_recent_turns(env):
    conversation = _conversation()
    for index in range(15):
        chat_repo.add_message(conversation['id'],
                              'user' if index % 2 == 0 else 'assistant',
                              f'旧消息{index}')
    routes._agent_ask(routes.AskIn(
        question='继续', web_mode='off', conversation_id=conversation['id']))
    history = env['calls'][-1]['args'][5]
    assert len(history) == 12
    assert history[-1] == {'role': 'user', 'content': '旧消息14'}


def test_explicit_client_history_wins_over_stored_history(env):
    conversation = _conversation()
    chat_repo.add_message(conversation['id'], 'user', '服务端旧消息')
    history = [{'role': 'user', 'content': '客户端历史'}]
    routes._agent_ask(routes.AskIn(
        question='优先级', web_mode='off', history=history,
        conversation_id=conversation['id']))
    assert env['calls'][-1]['args'][5] == history


def test_persist_failure_does_not_discard_the_answer(env, monkeypatch):
    conversation = _conversation()
    monkeypatch.setattr(routes.chat_repo, 'add_message',
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('disk full')))
    result = routes._agent_ask(routes.AskIn(
        question='写入失败', web_mode='off', conversation_id=conversation['id']))
    assert result['persisted'] is False
    assert result['text'] == '服务器回答正文'
    assert result['error_code'] == ''
    assert chat_repo.get_conversation(conversation['id'])['messages'] == []


def test_model_error_is_persisted_as_local_error_message(env, monkeypatch):
    failed = dict(ANSWER, ok=False, text='', error='模型超时', error_code='timeout')
    monkeypatch.setattr(routes.agent_chat, 'answer', lambda *args, **kwargs: failed)
    conversation = _conversation()
    result = routes._agent_ask(routes.AskIn(
        question='失败也要留痕', web_mode='off', conversation_id=conversation['id']))
    assert result['persisted'] is True
    assert result['error_code'] == 'timeout'
    saved = chat_repo.get_conversation(conversation['id'])
    assert saved['messages'][-1]['role'] == 'assistant'
    assert saved['messages'][-1]['engine'] == 'local'
    assert saved['messages'][-1]['error'] == '模型超时'


def test_http_ask_exposes_persisted_flags(env):
    conversation = _conversation()
    response = env['client'].post('/api/agent/ask', json={
        'question': 'HTTP 持久化', 'web_mode': 'off', 'conversation_id': conversation['id'],
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['persisted'] is True
    assert body['conversation_id'] == conversation['id']
    assert body['error_code'] == ''
    assert len(chat_repo.get_conversation(conversation['id'])['messages']) == 2


def test_http_stream_persists_when_conversation_id_given(env):
    conversation = _conversation()
    response = env['client'].post('/api/agent/ask/stream', json={
        'question': '流式持久化', 'web_mode': 'off', 'conversation_id': conversation['id'],
    })
    assert response.status_code == 200, response.text
    payloads = [json.loads(line[6:]) for line in response.text.splitlines()
                if line.startswith('data: ')]
    result = next(item['data'] for item in payloads if item.get('type') == 'result')
    assert result['persisted'] is True
    assert result['conversation_id'] == conversation['id']
    saved = chat_repo.get_conversation(conversation['id'])
    assert [message['content'] for message in saved['messages']] == ['流式持久化', '服务器回答正文']
