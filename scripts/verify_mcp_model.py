"""Explicit fast-model acceptance: real chat.answer + real official SDK stdio.

Only synthetic context is sent. Personal/database tools are blocked, the role
text is synthetic, and APP_DIR/MCP configuration are temporary. Model settings
are read only by llm. No provider identifiers, errors, arguments or replies are
printed. Default execution never contacts a model or starts an MCP process.
"""
import argparse
from contextlib import ExitStack
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
TOOL_NAME = 'mcp_acceptance_nonce'
QUESTION = ('Call the local synthetic nonce tool and include its exact returned '
            'marker in your final answer. The marker cannot be known without '
            'calling that tool. Do not query memory, agenda, manuals or websites.')
OUTCOMES = frozenset({'passed', 'not_requested', 'not_configured', 'sdk_missing',
                     'discovery_failed', 'model_timeout', 'model_http', 'model_network',
                     'model_json', 'model_invalid_message', 'model_model_error',
                     'tool_not_called', 'tool_failed', 'marker_missing',
                     'unexpected_tool', 'integration_error'})


def server_config():
    return {'id': 'acceptance', 'enabled': True, 'trusted': True,
            'command': sys.executable,
            'args': [str(ROOT / 'tests/fixtures/mcp_model_server.py')],
            'workspace': 'synthetic_mcp', 'course': 'synthetic_course',
            'network': False, 'public_data': False, 'timeout_seconds': 5,
            'tools': {'nonce': {'read_only': True,
                                'description': 'Return the synthetic acceptance nonce.'}}}


def verify(*, model_check=False, timeout_seconds=30, test_completion=None,
           service_count=1, selected_service=None):
    if service_count not in (1, 4) or selected_service not in (None, 'acceptance'):
        raise ValueError('Unsupported synthetic acceptance configuration')
    started = time.monotonic()
    report = {'schema_version': 1, 'run_id': uuid.uuid4().hex,
              'execution': 'not_run', 'layer': 'chat.answer + llm.chat_with_tools + MCP stdio',
              'model_tier': 'fast', 'total_model_budget_seconds': timeout_seconds,
              'synthetic_only': True, 'personal_db_access': False,
              'tools_exposed': [], 'invocations': [], 'trace': [],
              'marker_in_final_answer': False, 'model_selected_tool': False,
              'model_attempted': False, 'outcome': 'not_requested'}
    report.update(service_count=service_count, selected_service=selected_service or 'auto')

    def finish(outcome):
        assert outcome in OUTCOMES
        report['outcome'] = outcome
        report['real_model_acceptance'] = outcome == 'passed' and report['execution'] == 'real_model'
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        return report

    if not model_check:
        return finish('not_requested')
    from app.agent import llm
    if not llm.available('fast'):
        return finish('not_configured')
    if importlib.util.find_spec('mcp') is None:
        return finish('sdk_missing')
    from app.agent import chat
    from app.integrations import mcp_bridge as bridge
    from app.memory import db

    markers, failures, blocked = [], [], []
    original_loop, original_call = llm.chat_with_tools, bridge.ToolSet.call
    allowed_names = {TOOL_NAME, 'search_memory', 'query_agenda', 'query_manual'}

    def deny(*args, **kwargs):
        blocked.append(True)
        raise RuntimeError('synthetic_access_blocked')

    def observed_call(session, name, arguments):
        when = time.monotonic()
        value = original_call(session, name, arguments)
        marker = value.get('text', '')
        valid = bool(re.fullmatch(r'MCP_SYNTHETIC_[0-9a-f]{48}', marker))
        if valid:
            markers.append(marker)
        report['invocations'].append({
            'id': 'call_' + str(len(report['invocations']) + 1),
            'tool': name if name == TOOL_NAME else 'unexpected',
            'status': 'returned_marker' if valid else 'failed',
            'elapsed_seconds': round(time.monotonic() - when, 3),
            'marker_sha256': hashlib.sha256(marker.encode()).hexdigest() if valid else None})
        return value

    def bounded_loop(tier, messages, tools, executor, **kwargs):
        report['tools_exposed'] = sorted({
            tool['function']['name'] if tool['function']['name'] in allowed_names else 'unexpected'
            for tool in tools})
        if TOOL_NAME not in report['tools_exposed'] or 'unexpected' in report['tools_exposed']:
            failures.append('discovery_failed')
            raise llm.LLMError('Synthetic tool unavailable')
        report['model_attempted'] = True
        report['execution'] = 'deterministic_mock' if test_completion else 'real_model'
        # Tool choice remains auto; no forced function or fake executor.
        kwargs.update(force_tool=False, timeout_seconds=timeout_seconds, temperature=0)
        try:
            return original_loop(tier, messages, tools, executor, **kwargs)
        except llm.LLMError as exc:
            failures.append('model_' + exc.code)
            raise

    try:
        with tempfile.TemporaryDirectory(prefix='mindock-mcp-model-') as directory, ExitStack() as stack:
            temporary = Path(directory)
            path = temporary / 'extensions/mcp.json'
            path.parent.mkdir()
            servers = [{**server_config(), 'id': f'other{index}'}
                       for index in range(service_count - 1)] + [server_config()]
            path.write_text(json.dumps({'servers': servers}), encoding='utf-8')
            stack.enter_context(patch.object(db, 'APP_DIR', temporary))
            stack.enter_context(patch.object(db, 'DB_PATH', temporary / 'unused.sqlite'))
            # Guard even imported aliases of db.connect via the sqlite boundary.
            stack.enter_context(patch.object(sqlite3, 'connect', deny))
            if test_completion is not None:
                stack.enter_context(patch.object(llm, '_bounded_completion', test_completion))
            stack.enter_context(patch.object(chat, 'system_for', return_value='You answer synthetic acceptance questions.'))
            for owner, name in [(chat.repo, 'search'), (chat.repo, 'query_agenda'),
                                (chat.sources, 'search'), (chat.platform_manual, 'search'),
                                (chat.web_search, 'search'), (chat.web_search, 'open_page')]:
                stack.enter_context(patch.object(owner, name, deny))
            stack.enter_context(patch.object(bridge.ToolSet, 'call', observed_call))
            stack.enter_context(patch.object(llm, 'chat_with_tools', bounded_loop))
            result = chat.answer(QUESTION, [], tier='fast', history=[], agenda=None,
                                 web_results=[], personal_context=None, web_mode='off',
                                 workspace='synthetic_mcp', scope_label='synthetic_course',
                                 **({'mcp_server_id': selected_service} if selected_service else {}))
            for index, entry in enumerate(result.get('trace', []), 1):
                if entry.get('type') != 'tool':
                    continue
                name = entry.get('tool')
                report['trace'].append({'id': 'trace_' + str(index),
                    'tool': name if name in allowed_names else 'unexpected',
                    'status': entry.get('status') if entry.get('status') in {'done', 'error'} else 'unknown'})
            report['model_selected_tool'] = any(
                row['tool'] == TOOL_NAME for row in report['trace'])
            report['marker_in_final_answer'] = any(marker in result.get('text', '') for marker in markers)
            if blocked or any(row['tool'] != TOOL_NAME for row in report['trace']):
                return finish('unexpected_tool')
            if failures:
                return finish(failures[0])
            if not report['model_selected_tool']:
                return finish('tool_not_called')
            if not markers:
                return finish('tool_failed')
            if not result.get('ok') or not report['marker_in_final_answer']:
                return finish('marker_missing')
            return finish('passed')
    except Exception:
        return finish('integration_error')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-check', action='store_true')
    parser.add_argument('--selected-service-check', action='store_true',
                        help='Configure four synthetic services and select only the fourth')
    parser.add_argument('--timeout-seconds', type=float, default=30,
                        help='Total model loop budget, including retries and tool elapsed time (default: 30)')
    args = parser.parse_args(argv)
    if not 0 < args.timeout_seconds <= 120:
        parser.error('timeout must be in (0, 120]')
    sys.path.insert(0, str(ROOT))
    report = verify(model_check=args.model_check, timeout_seconds=args.timeout_seconds,
                    service_count=4 if args.selected_service_check else 1,
                    selected_service='acceptance' if args.selected_service_check else None)
    print(json.dumps(report, indent=2))
    return 0 if report['outcome'] == 'passed' else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print('{"outcome":"integration_error"}')
        raise SystemExit(1)
