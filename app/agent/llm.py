"""CyberPal · 模型层。

只有一个职责：把「一段消息 + 检索到的记忆」送进外部模型服务，拿回一段回答。
走 OpenAI 兼容的 /chat/completions（DeepSeek、通义、智谱、Kimi、OpenAI、本地
Ollama/vLLM 都是这套），所以不用绑任何 SDK —— 标准库 urllib 足够。

两个档位：
    fast       日常问答、检索作答
    reasoning  论文、论证、复杂分析

密钥只从这里读，不回传前端。
"""

from __future__ import annotations

import json
import math
import multiprocessing
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Iterator

import tenacity

from ..settings import store as settings_store
from ..settings import model_profiles

# 本机调试时 http_proxy 会拦请求，模型服务必须绕开代理直连
_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=ssl.create_default_context()),
)

TIMEOUT = 90
_RETRYABLE_HTTP_CODES = frozenset({429, 502, 503, 504})
HTTP_STATUS_BUCKETS = frozenset({'http_4xx', 'http_5xx', 'http_unknown'})


class LLMError(RuntimeError):
    """模型层错误，带可读的中文原因，前端能直接展示。"""

    def __init__(self, message, *, code='model_error', http_status_bucket='http_unknown',
                 response_metadata=None):
        super().__init__(message)
        self.code = code if code in SAFE_ERROR_CODES else 'model_error'
        self.response_metadata = safe_response_metadata(response_metadata)
        self.http_status_bucket = (
            http_status_bucket
            if isinstance(http_status_bucket, str) and http_status_bucket in HTTP_STATUS_BUCKETS
            else 'http_unknown'
        )


SAFE_ERROR_CODES = frozenset({'timeout', 'http', 'network', 'json',
                              'invalid_message', 'cancelled', 'model_error'})


_METADATA_FIELDS = ('response', 'choices', 'message', 'content', 'reasoning_content', 'tool_calls')
_METADATA_TYPES = frozenset({'dict', 'list', 'str', 'int', 'float', 'bool', 'null', 'other'})
_FINISH_REASONS = frozenset({'stop', 'length', 'tool_calls', 'function_call', 'content_filter', 'unknown'})


def safe_response_metadata(metadata) -> dict:
    """Project only known structural metadata, even from an altered exception."""
    if type(metadata) is not dict:
        return {}
    result = {}
    for field in _METADATA_FIELDS:
        value = metadata.get(field + '_type')
        if type(value) is str and value in _METADATA_TYPES:
            result[field + '_type'] = value
        value = metadata.get(field + '_present')
        if type(value) is bool:
            result[field + '_present'] = value
        value = metadata.get(field + '_length')
        if type(value) is int and 0 <= value <= 4 * 1024 * 1024 + 1:
            result[field + '_length'] = value
    reason = metadata.get('finish_reason')
    if type(reason) is str and reason in _FINISH_REASONS:
        result['finish_reason'] = reason
    return result


def _response_metadata(data) -> dict:
    """Never copy provider values or unknown field names into diagnostics."""
    result = {}

    def describe(field, value):
        kind = 'null' if value is None else (
            type(value).__name__ if type(value) in (dict, list, str, int, float, bool) else 'other'
        )
        result[field + '_type'] = kind
        if type(value) in (dict, list, str):
            result[field + '_length'] = len(value)

    describe('response', data)
    if type(data) is dict:
        choices = data.get('choices')
        describe('choices', choices)
        if type(choices) is list and choices and type(choices[0]) is dict:
            choice = choices[0]
            reason = choice.get('finish_reason')
            result['finish_reason'] = (
                reason if type(reason) is str and reason in _FINISH_REASONS else 'unknown'
            )
            message = choice.get('message')
            describe('message', message)
            if type(message) is dict:
                for field in ('content', 'reasoning_content', 'tool_calls'):
                    result[field + '_present'] = field in message
                    describe(field, message.get(field))
    return safe_response_metadata(result)


def _http_status_bucket(status: int) -> str:
    if 400 <= status <= 499:
        return 'http_4xx'
    if 500 <= status <= 599:
        return 'http_5xx'
    return 'http_unknown'


def _deadline(timeout_seconds):
    if timeout_seconds is None:
        return None
    if isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError('timeout_seconds must be finite and positive')
    return time.monotonic() + timeout_seconds


def _remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise LLMError('Model deadline exceeded', code='timeout')
    return value


def _check_cancel(should_cancel):
    """Cooperative cancellation: the ask budget owner flips this from another thread."""
    if should_cancel is not None and should_cancel():
        raise LLMError('请求已取消', code='cancelled')


def _completion_worker(pipe, url, body, headers, budget):
    """Killable transport owner: DNS, TLS, retry and all body reads live here."""
    try:
        deadline = time.monotonic() + budget
        req = urllib.request.Request(url, data=json.dumps(body).encode('utf-8'),
                                     headers=headers, method='POST')
        with _open_completion(req, deadline=deadline) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                pipe.send(('error', 'invalid_message', 'http_unknown'))
                return
            data = json.loads(raw.decode('utf-8'))
        pipe.send(('ok', data))
    except urllib.error.HTTPError as exc:
        exc.close()  # Never read or forward provider error bodies.
        pipe.send(('error', 'http', _http_status_bucket(exc.code)))
    except urllib.error.URLError as exc:
        pipe.send(('error', 'timeout' if isinstance(exc.reason, TimeoutError) else 'network', 'http_unknown'))
    except TimeoutError:
        pipe.send(('error', 'timeout', 'http_unknown'))
    except (ValueError, UnicodeError):
        pipe.send(('error', 'json', 'http_unknown'))
    except LLMError as exc:
        pipe.send(('error', exc.code, exc.http_status_bucket))
    except Exception:
        pipe.send(('error', 'model_error', 'http_unknown'))
    finally:
        pipe.close()


def _bounded_completion(url, body, headers, deadline, should_cancel=None):
    # A socket timeout only bounds idle waits. A process deadline also bounds
    # slow trickle responses and DNS, without leaving a live request thread.
    _check_cancel(should_cancel)
    context = multiprocessing.get_context('spawn')
    reader, writer = context.Pipe(duplex=False)
    process = context.Process(target=_completion_worker,
                              args=(writer, url, body, headers, _remaining(deadline)),
                              daemon=True)
    try:
        process.start()
        writer.close()
        while not reader.poll(min(0.2, _remaining(deadline))):
            _check_cancel(should_cancel)
        message = reader.recv()
        if (not isinstance(message, tuple) or len(message) != 3
                or message[0] != 'error'):
            if isinstance(message, tuple) and len(message) == 2 and message[0] == 'ok':
                status, value = message
                _remaining(deadline)
                return value
            raise LLMError('Model transport failed', code='network') from None
        _, code, bucket = message
        code = code if isinstance(code, str) and code in SAFE_ERROR_CODES else 'model_error'
        bucket = bucket if isinstance(bucket, str) and bucket in HTTP_STATUS_BUCKETS else 'http_unknown'
        _remaining(deadline)
        raise LLMError('Model request failed', code=code, http_status_bucket=bucket)
    except (EOFError, OSError):
        raise LLMError('Model transport failed', code='network') from None
    finally:
        reader.close()
        writer.close()
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=0.2)
            if process.is_alive():
                process.kill()
                process.join(timeout=0.2)
            process.close()


_NO_FINAL_ANSWER_MESSAGE = '模型服务未返回最终答案，请检查模型接口或更换模型后重试。'


def _message(data):
    try:
        if not isinstance(data, dict) or not isinstance(data.get('choices'), list):
            raise ValueError
        message = data['choices'][0]['message']
        if not isinstance(message, dict):
            raise ValueError
        if message.get('content') is not None and not isinstance(message['content'], str):
            raise ValueError
        calls = message.get('tool_calls') or []
        if not isinstance(calls, list):
            raise ValueError
        for call in calls:
            if (not isinstance(call, dict) or not isinstance(call.get('id'), str)
                    or not isinstance(call.get('function'), dict)
                    or not isinstance(call['function'].get('name'), str)
                    or not isinstance(call['function'].get('arguments'), str)):
                raise ValueError
        if 'content' not in message and not calls:
            if 'reasoning_content' in message:
                raise LLMError(_NO_FINAL_ANSWER_MESSAGE, code='invalid_message',
                               response_metadata=_response_metadata(data)) from None
            raise ValueError
        return message
    except (KeyError, IndexError, TypeError, ValueError):
        raise LLMError('Invalid model message', code='invalid_message',
                       response_metadata=_response_metadata(data)) from None


def _answer(data) -> str:
    message = _message(data)
    content = message.get('content')
    if not isinstance(content, str) or not content.strip():
        raise LLMError(_NO_FINAL_ANSWER_MESSAGE, code='invalid_message',
                       response_metadata=_response_metadata(data)) from None
    return content


def _bounded(url, body, headers, deadline, should_cancel):
    """Call the killable transport, only forwarding the cancel hook when set.

    Keeping the legacy four-argument call shape matters for callers and tests
    that monkeypatch _bounded_completion without a cancellation parameter.
    """
    if should_cancel is None:
        return _bounded_completion(url, body, headers, deadline)
    return _bounded_completion(url, body, headers, deadline, should_cancel=should_cancel)


def _retryable_failure(retry_state):
    """Retry transport problems and temporary upstream errors, never bad credentials."""
    outcome = retry_state.outcome
    if outcome is None or not outcome.failed:
        return False
    exc = outcome.exception()
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in _RETRYABLE_HTTP_CODES
    return isinstance(exc, (urllib.error.URLError, TimeoutError))


def _close_retried_failure(retry_state):
    """Drop the provider error body before retrying instead of reading it."""
    outcome = retry_state.outcome
    if outcome is not None and outcome.failed:
        exc = outcome.exception()
        if isinstance(exc, urllib.error.HTTPError):
            exc.close()


def _open_completion(request, deadline=None):
    """Open the completion request, retrying temporary failures once.

    tenacity owns the retry loop, and the backoff shares the request's overall
    deadline: every socket timeout and sleep is clamped to the remaining budget,
    so retries can never extend /ask past its deadline.
    """
    def attempt():
        return _OPENER.open(
            request, timeout=TIMEOUT if deadline is None else _remaining(deadline)
        )

    def wait_for_retry(_retry_state):
        return .5 if deadline is None else min(.5, _remaining(deadline))

    retrying = tenacity.Retrying(
        retry=_retryable_failure,
        stop=tenacity.stop_after_attempt(2),
        wait=wait_for_retry,
        before_sleep=_close_retried_failure,
        reraise=True,
    )
    return retrying(attempt)


def tier_of(role: dict | None) -> str:
    """角色的模型档位。论文/论证类走 reasoning，其余走 fast。"""
    if role and role.get("model") in ("reasoning", "fast"):
        return role["model"]
    return "fast"


def _endpoint(base_url: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise LLMError("接口地址为空")
    # 用户可能填的是 /v1 或完整路径，两种都接住
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def _model_config(tier: str) -> dict:
    try:
        return model_profiles.resolve(settings_store.read_raw(), tier)
    except (ValueError, TypeError, KeyError, OSError, RecursionError):
        raise LLMError('模型配置或档位绑定无效，请到设置重新检查') from None


def available(tier: str = "fast") -> bool:
    try:
        cfg = _model_config(tier)
        return bool(cfg.get("base_url") and cfg.get("model"))
    except LLMError:
        return False


def _payload(tier: str, messages: list[dict], temperature: float,
             stream: bool, response_format: dict | None = None) -> tuple[str, dict, dict]:
    cfg = _model_config(tier)
    if not (cfg.get("base_url") and cfg.get("model")):
        raise LLMError(f"「{tier}」档还没配好：请到 设置 → 模型与接口 填接口地址与模型名")

    url = _endpoint(cfg["base_url"])
    body = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": temperature,
        "stream": stream,
    }
    if response_format is not None:
        body["response_format"] = response_format
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
    }
    if cfg.get("api_key"):
        headers["Authorization"] = "Bearer " + cfg["api_key"]
    return url, body, headers


def _describe_http_error(e: urllib.error.HTTPError) -> str:
    """把上游的报错翻成人话，别让前端只说一句 500。"""
    detail = ""
    try:
        raw = e.read().decode("utf-8", "replace")
        data = json.loads(raw)
        detail = (data.get("error") or {}).get("message") or data.get("message") or raw
    except Exception:
        detail = ""
    hints = {
        401: "密钥无效或没填",
        403: "密钥没有这个模型的权限",
        404: "接口地址或模型名不对（检查是否漏了 /v1）",
        429: "被限流或余额不足",
    }
    hint = hints.get(e.code, "")
    return f"模型服务返回 {e.code}{'（' + hint + '）' if hint else ''}：{detail[:300]}"


def chat(tier: str, messages: list[dict], temperature: float = 0.3,
         response_format: dict | None = None, *, timeout_seconds: float | None = None,
         should_cancel: Callable[[], bool] | None = None, max_tokens: int | None = None) -> str:
    """一次同步问答，返回回答正文。"""
    if max_tokens is not None and (
        isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 1 <= max_tokens <= 32768
    ):
        raise ValueError('max_tokens must be an integer between 1 and 32768')
    _check_cancel(should_cancel)
    deadline = _deadline(timeout_seconds)
    url, body, headers = _payload(
        tier, messages, temperature, stream=False, response_format=response_format
    )
    if max_tokens is not None:
        body['max_tokens'] = max_tokens
    if deadline is not None:
        data = _bounded(url, body, headers, deadline, should_cancel)
        _check_cancel(should_cancel)
        return _answer(data)
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with _open_completion(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        status_bucket = _http_status_bucket(e.code)
        raise LLMError(_describe_http_error(e), http_status_bucket=status_bucket) from e
    except urllib.error.URLError as e:
        raise LLMError(
            f"连不上模型服务（{e.reason}）。检查接口地址、网络，或服务商是否要求代理。"
        ) from e
    except json.JSONDecodeError as e:
        raise LLMError("模型服务返回的不是 JSON，可能地址填错了") from e

    return _answer(data)


def chat_with_tools(
    tier: str,
    messages: list[dict],
    tools: list[dict],
    executor: Callable[[str, dict], dict],
    *,
    force_tool: bool = False,
    max_rounds: int = 3,
    temperature: float = 0.3,
    progress: Callable[[dict], None] | None = None,
    review_completion: Callable[[str], str] | None = None,
    timeout_seconds: float | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[str, list[dict], str]:
    """Run a tool loop. Optional budget spans all rounds/retries and final chat.

    Bounded calls use non-streaming transport, including with progress enabled.
    Executors/review callbacks remain synchronous and must bound their own IO;
    their elapsed time consumes this budget and is checked before further IO.
    """
    _check_cancel(should_cancel)
    deadline = _deadline(timeout_seconds)
    current = list(messages)
    web_results: list[dict] = []
    tool_error = ''
    for _ in range(max(1, max_rounds)):
        _check_cancel(should_cancel)
        if deadline is not None:
            _remaining(deadline)
        if progress:
            progress({'type': 'status', 'message': '正在理解问题与整理资料'})
        url, body, headers = _payload(tier, current, temperature, stream=bool(progress) and deadline is None)
        body['tools'] = tools
        body['tool_choice'] = (
            {'type': 'function', 'function': {'name': tools[0]['function']['name']}}
            if force_tool and tools else 'auto'
        )
        req = urllib.request.Request(
            url, data=json.dumps(body).encode('utf-8'), headers=headers, method='POST'
        )
        try:
            if deadline is not None:
                data = _bounded(url, body, headers, deadline, should_cancel)
                _check_cancel(should_cancel)
            else:
                with _open_completion(req) as resp:
                    content_type = resp.headers.get('Content-Type', '') if hasattr(resp, 'headers') else ''
                    if 'text/event-stream' in content_type:
                        data = _read_tool_stream(resp, progress)
                    else:
                        data = json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            raise LLMError(_describe_http_error(e), http_status_bucket=_http_status_bucket(e.code)) from e
        except urllib.error.URLError as e:
            raise LLMError(f"连不上模型服务（{e.reason}）") from e
        except TimeoutError as e:
            raise LLMError('模型响应超时；已取得的来源保留在本轮下方，可重试') from e
        except json.JSONDecodeError as e:
            raise LLMError('模型服务返回的不是 JSON，可能地址填错了') from e

        if deadline is not None:
            _message(data)
        try:
            message = data['choices'][0]['message']
        except (KeyError, IndexError) as e:
            raise LLMError(
                f"模型返回结构不认识：{json.dumps(data, ensure_ascii=False)[:300]}"
            ) from e
        calls = message.get('tool_calls') or []
        if not calls:
            _check_cancel(should_cancel)
            feedback = review_completion(message.get('content') or '') if review_completion else ''
            if deadline is not None:
                _remaining(deadline)
            if feedback:
                current.append({'role': 'assistant', 'content': message.get('content') or ''})
                current.append({'role': 'user', 'content': feedback})
                force_tool = False
                continue
            return message.get('content') or '', web_results, tool_error
        current.append({k: v for k, v in message.items() if k in {'role', 'content', 'tool_calls', 'reasoning_content'}})
        for call in calls:
            _check_cancel(should_cancel)
            if deadline is not None:
                _remaining(deadline)
            fn = call.get('function') or {}
            name = fn.get('name') or ''
            try:
                args = json.loads(fn.get('arguments') or '{}')
                result = executor(name, args)
            except Exception as exc:
                result = {'error': 'tool_error' if deadline is not None else str(exc)}
            _check_cancel(should_cancel)
            if deadline is not None:
                _remaining(deadline)
            if name in {'web_search', 'web_open'} and isinstance(result, dict) and result.get('results'):
                web_results.extend(result['results'])
            if isinstance(result, dict) and result.get('error'):
                tool_error = str(result['error'])
            current.append({
                'role': 'tool',
                'tool_call_id': call.get('id', ''),
                'name': name,
                'content': json.dumps(result, ensure_ascii=False),
            })
        force_tool = False
    current.append({'role': 'user', 'content': '本轮工具预算已用完。请根据已经拿到的证据直接回答原问题；区分已核实与未核实，不承诺稍后执行。'})
    kwargs = {}
    if deadline is not None:
        kwargs['timeout_seconds'] = _remaining(deadline)
    if should_cancel is not None:
        kwargs['should_cancel'] = should_cancel
    return chat(tier, current, temperature, **kwargs), web_results, tool_error


def _read_tool_stream(response, progress=None) -> dict:
    """Accumulate fragmented tool arguments while forwarding visible text deltas."""
    message = {'role': 'assistant', 'content': ''}
    calls = {}
    for raw in response:
        line = raw.decode('utf-8', 'replace').strip()
        if not line.startswith('data:'):
            continue
        data = line[5:].strip()
        if data == '[DONE]':
            break
        try:
            chunk = json.loads(data)
        except ValueError as exc:
            raise LLMError('模型流返回无效数据，请重试') from exc
        if chunk.get('error'):
            raise LLMError('模型流返回错误，请检查接口服务')
        for choice in chunk.get('choices', []):
            delta = choice.get('delta') or {}
            if delta.get('content'):
                message['content'] += delta['content']
                if progress:
                    progress({'type': 'delta', 'text': delta['content']})
            if delta.get('reasoning_content'):
                message['reasoning_content'] = message.get('reasoning_content', '') + delta['reasoning_content']
            for item in delta.get('tool_calls') or []:
                call = calls.setdefault(item.get('index', 0), {
                    'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}
                })
                if item.get('id'):
                    call['id'] = item['id']
                for field in ('name', 'arguments'):
                    call['function'][field] += (item.get('function') or {}).get(field) or ''
    if calls:
        message['tool_calls'] = [calls[i] for i in sorted(calls)]
    return {'choices': [{'message': message}]}


def chat_stream(tier: str, messages: list[dict],
                temperature: float = 0.3) -> Iterator[str]:
    """流式问答，逐段吐字。上游不支持流式就退化成一次性返回。"""
    url, body, headers = _payload(tier, messages, temperature, stream=True)
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        resp = _OPENER.open(req, timeout=TIMEOUT)
    except urllib.error.HTTPError as e:
        raise LLMError(_describe_http_error(e), http_status_bucket=_http_status_bucket(e.code)) from e
    except urllib.error.URLError as e:
        raise LLMError(f"连不上模型服务（{e.reason}）") from e

    with resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line or not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                break
            try:
                obj = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            try:
                piece = obj["choices"][0]["delta"].get("content")
            except (KeyError, IndexError):
                piece = None
            if piece:
                yield piece


def probe(tier: str = "fast") -> dict[str, Any]:
    """连通性自检：真发一次最小请求，让「配置不了模型」有个明确结论。"""
    out: dict[str, Any] = {
        "tier": tier,
        "base_url": "",
        "model": "",
        "key_set": False,
    }
    try:
        cfg = _model_config(tier)
        out.update(base_url=cfg.get('base_url', ''), model=cfg.get('model', ''),
                   key_set=bool(cfg.get('api_key')))
        reply = chat(tier, [{"role": "user", "content": "回复两个字：正常"}], 0)
        out.update(ok=True, reply=(reply or "")[:60])
    except LLMError as e:
        out.update(ok=False, error=str(e))
    return out
