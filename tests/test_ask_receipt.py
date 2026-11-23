"""B1.1/P2-6: /ask 持久化回执区分 user_saved / assistant_saved。

前端只有在 user_saved=False 时才补写用户消息；persisted 保持旧语义
（user 与 assistant 都落库），老客户端无需改动。
"""
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
    monkeypatch.setattr(routes.agent_chat, 'answer', lambda *args, **kwargs: dict(ANSWER))
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        yield client


def _conversation():
    return chat_repo.create_conversation()


def _ask(**kwargs):
    return routes._agent_ask(routes.AskIn(**kwargs))


def _fail_assistant_only(monkeypatch):
    real_add_message = chat_repo.add_message

    def flaky(conv_id, role, content, **kwargs):
        if role == 'assistant':
            raise RuntimeError('assistant write failed')
        return real_add_message(conv_id, role, content, **kwargs)

    monkeypatch.setattr(routes.chat_repo, 'add_message', flaky)


def test_both_messages_saved_keeps_legacy_persisted(env):
    conversation = _conversation()
    result = _ask(question='双写成功', web_mode='off',
                  conversation_id=conversation['id'])
    assert result['user_saved'] is True
    assert result['assistant_saved'] is True
    assert result['persisted'] is True
    saved = chat_repo.get_conversation(conversation['id'])
    assert [message['role'] for message in saved['messages']] == ['user', 'assistant']
    assert saved['title'] == '双写成功'


def test_assistant_failure_keeps_user_saved_true(env, monkeypatch):
    _fail_assistant_only(monkeypatch)
    conversation = _conversation()
    result = _ask(question='assistant 写失败', web_mode='off',
                  conversation_id=conversation['id'])
    assert result['user_saved'] is True
    assert result['assistant_saved'] is False
    assert result['persisted'] is False
    assert result['text'] == ANSWER['text']
    saved = chat_repo.get_conversation(conversation['id'])
    assert [message['role'] for message in saved['messages']] == ['user']
    # autotitle 在 user_saved 后执行，仍然生效。
    assert saved['title'] == 'assistant 写失败'


def test_user_failure_saves_neither_message(env, monkeypatch):
    monkeypatch.setattr(
        routes.chat_repo, 'add_message',
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('db down')))
    conversation = _conversation()
    result = _ask(question='user 写失败', web_mode='off',
                  conversation_id=conversation['id'])
    assert result['user_saved'] is False
    assert result['assistant_saved'] is False
    assert result['persisted'] is False
    assert chat_repo.get_conversation(conversation['id'])['messages'] == []


def test_without_conversation_id_receipts_are_false(env):
    result = _ask(question='不落库', web_mode='off')
    assert result['user_saved'] is False
    assert result['assistant_saved'] is False
    assert result['persisted'] is False
    assert result['conversation_id'] is None


def test_llm_error_branch_exposes_receipt_fields(env, monkeypatch):
    def raise_error(*args, **kwargs):
        raise routes.llm.LLMError('模型超时', code='timeout')

    monkeypatch.setattr(routes.agent_chat, 'answer', raise_error)
    result = _ask(question='模型超时', web_mode='off')
    assert result['error_code'] == 'timeout'
    assert result['user_saved'] is False
    assert result['assistant_saved'] is False
    assert result['persisted'] is False


def test_http_response_exposes_receipt_flags(env, monkeypatch):
    _fail_assistant_only(monkeypatch)
    conversation = _conversation()
    response = env.post('/api/agent/ask', json={
        'question': '回执', 'web_mode': 'off', 'conversation_id': conversation['id'],
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['user_saved'] is True
    assert body['assistant_saved'] is False
    assert body['persisted'] is False
