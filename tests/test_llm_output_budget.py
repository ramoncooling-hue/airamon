import io
import json
from unittest.mock import Mock

import pytest

from app.agent import llm


@pytest.fixture
def transport(monkeypatch):
    response = {'choices': [{'message': {'content': 'synthetic'}}]}
    config = Mock(return_value={
        'fast': {'base_url': 'https://example.test/v1', 'model': 'test-model'},
    })
    ordinary = Mock(side_effect=lambda request: io.BytesIO(json.dumps(response).encode('utf-8')))
    bounded = Mock(return_value=response)
    monkeypatch.setattr(llm.settings_store, 'read_raw', config)
    monkeypatch.setattr(llm, '_open_completion', ordinary)
    monkeypatch.setattr(llm, '_bounded_completion', bounded)
    monkeypatch.setattr(llm._OPENER, 'open', Mock(side_effect=AssertionError('no real network')))
    monkeypatch.setattr(llm.time, 'monotonic', lambda: 100.0)
    return config, ordinary, bounded


@pytest.mark.parametrize('timeout_seconds', [None, 45], ids=['ordinary', 'deadline'])
@pytest.mark.parametrize('response_format', [None, {'type': 'json_object'}])
@pytest.mark.parametrize('budget_kwargs', [
    pytest.param({}, id='omitted'),
    pytest.param({'max_tokens': None}, id='none'),
    pytest.param({'max_tokens': 1}, id='minimum'),
    pytest.param({'max_tokens': 1800}, id='mapping-budget'),
    pytest.param({'max_tokens': 32768}, id='maximum'),
])
def test_chat_output_budget_preserves_request_body(transport, timeout_seconds, response_format, budget_kwargs):
    config, ordinary, bounded = transport
    messages = [{'role': 'user', 'content': 'map synthetic source'}]

    assert llm.chat('fast', messages, temperature=.2, response_format=response_format,
                    timeout_seconds=timeout_seconds, **budget_kwargs) == 'synthetic'

    expected = {'model': 'test-model', 'messages': messages, 'temperature': .2, 'stream': False}
    if response_format is not None:
        expected['response_format'] = response_format
    if budget_kwargs.get('max_tokens') is not None:
        expected['max_tokens'] = budget_kwargs['max_tokens']
    expected_headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
    config.assert_called_once_with()
    if timeout_seconds is None:
        bounded.assert_not_called()
        ordinary.assert_called_once()
        request, = ordinary.call_args.args
        assert request.full_url == 'https://example.test/v1/chat/completions'
        assert request.get_method() == 'POST'
        assert request.data == json.dumps(expected).encode('utf-8')
        assert dict(request.header_items()) == {
            key.capitalize(): value for key, value in expected_headers.items()
        }
    else:
        ordinary.assert_not_called()
        bounded.assert_called_once_with('https://example.test/v1/chat/completions', expected,
                                        expected_headers, 145.0)


@pytest.mark.parametrize('timeout_seconds', [None, 45], ids=['ordinary', 'deadline'])
@pytest.mark.parametrize('max_tokens', [
    0, -1, True, False, 1.0, 1800.0, float('inf'), float('nan'), '1800', 32769,
])
def test_invalid_output_budget_fails_before_config_or_transport(monkeypatch, transport,
                                                               timeout_seconds, max_tokens):
    config, ordinary, bounded = transport
    payload = Mock(side_effect=AssertionError('no payload or configuration access'))
    config.side_effect = AssertionError('no personal configuration access')
    monkeypatch.setattr(llm, '_payload', payload)

    with pytest.raises(ValueError, match='max_tokens'):
        llm.chat('fast', [], max_tokens=max_tokens, timeout_seconds=timeout_seconds)

    payload.assert_not_called()
    config.assert_not_called()
    ordinary.assert_not_called()
    bounded.assert_not_called()


@pytest.mark.parametrize('timeout_seconds', [None, 45], ids=['ordinary', 'deadline'])
def test_output_budget_works_with_legacy_payload_mock(monkeypatch, transport, timeout_seconds):
    config, ordinary, bounded = transport
    body = {'model': 'legacy-model', 'messages': [], 'temperature': .3, 'stream': False}

    def legacy_payload(tier, messages, temperature, stream, response_format=None):
        assert (tier, messages, temperature, stream, response_format) == ('fast', [], .3, False, None)
        return 'https://example.test/v1/chat/completions', body, {}

    monkeypatch.setattr(llm, '_payload', legacy_payload)

    assert llm.chat('fast', [], max_tokens=1800, timeout_seconds=timeout_seconds) == 'synthetic'

    config.assert_not_called()
    assert body['max_tokens'] == 1800
    if timeout_seconds is None:
        bounded.assert_not_called()
        assert json.loads(ordinary.call_args.args[0].data) == body
    else:
        ordinary.assert_not_called()
        bounded.assert_called_once_with('https://example.test/v1/chat/completions', body, {}, 145.0)
