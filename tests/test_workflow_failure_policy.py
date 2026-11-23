"""T2.7/O-TEST-2: workflows/service.py 失败策略、审批、导出与后台轮询边界。

配合 T2.6 后台化语义：on_error=pause 停在失败步骤并置 run.status=error（可
retry_run 恢复）；retry_once 最多执行两次；skip 跳过失败步骤继续；approval
等待后由 approve_run_async 续跑；export_config 只导出定义，不含运行上下文。
"""

import json
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from app.hub import registry
from app.memory import db
from app.workflows import WorkflowService


def wait_until(predicate, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError('condition was not met in time')


class WorkflowFailurePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.skills = self.root / 'skills'
        self.agents = self.root / 'agents'
        self.skills.mkdir()
        self.agents.mkdir()
        self.patches = [
            patch.object(db, 'APP_DIR', self.root / 'app'),
            patch.object(db, 'DB_PATH', self.root / 'app' / 'test.db'),
            patch.object(registry, 'SKILLS_DIR', self.skills),
            patch.object(registry, 'AGENTS_DIR', self.agents),
            patch('app.runtime.data_dir', return_value=self.root),
        ]
        for active_patch in self.patches:
            active_patch.start()
            self.addCleanup(active_patch.stop)
        db.apply_schema()
        self.add_agent('agent-a')
        self.add_agent('agent-b')
        self.service = WorkflowService()

    def add_agent(self, key):
        directory = self.agents / key
        directory.mkdir()
        (directory / 'system.md').write_text(f'# {key}\n', encoding='utf-8')
        (directory / 'manifest.json').write_text(json.dumps({
            'key': key, 'name': key, 'version': '1.0.0', 'skills': [],
        }), encoding='utf-8')
        registry.sync()

    def flow(self, key, steps):
        self.service.create({'key': key, 'name': key})
        for step in steps:
            self.service.add_step(key, step)
        return key

    def test_pause_policy_marks_run_error_and_stops_later_steps(self):
        self.flow('paused', [
            {'key': 'first', 'kind': 'agent', 'agent_key': 'agent-a', 'on_error': 'pause'},
            {'key': 'second', 'kind': 'agent', 'agent_key': 'agent-b'},
        ])
        calls = []

        def failing(question, hits, **kwargs):
            calls.append(question)
            raise RuntimeError('provider exploded')

        with patch('app.workflows.service.agent_chat.answer', side_effect=failing):
            run = self.service.start_run('paused', {'question': 'q'})

        self.assertEqual(run['status'], 'error')
        self.assertEqual(run['error'], 'provider exploded')
        self.assertTrue(run['finished_at'])
        self.assertEqual(run['step_statuses'], ['error', 'pending'])
        self.assertEqual(run['steps'][0]['attempt'], 1)
        self.assertEqual(len(calls), 1, 'pause 后不应执行后续步骤')

    def test_retry_once_runs_twice_then_succeeds(self):
        self.flow('retried', [
            {'key': 'first', 'kind': 'agent', 'agent_key': 'agent-a', 'on_error': 'retry_once'},
        ])
        calls = []

        def flaky(question, hits, **kwargs):
            calls.append(question)
            if len(calls) == 1:
                raise RuntimeError('transient failure')
            return {'ok': True, 'text': 'recovered'}

        with patch('app.workflows.service.agent_chat.answer', side_effect=flaky):
            run = self.service.start_run('retried', {'question': 'q'})

        self.assertEqual(run['status'], 'success')
        self.assertEqual(len(calls), 2)
        self.assertEqual(run['steps'][0]['attempt'], 2)
        self.assertEqual(run['retried_step_keys'], ['first'])
        self.assertEqual(run['executed_step_keys'], ['first'])
        self.assertEqual(run['error'], '')

    def test_retry_once_gives_up_and_retry_run_recovers(self):
        self.flow('give-up', [
            {'key': 'first', 'kind': 'agent', 'agent_key': 'agent-a', 'on_error': 'retry_once'},
        ])

        def always_fail(question, hits, **kwargs):
            raise RuntimeError('still broken')

        with patch('app.workflows.service.agent_chat.answer', side_effect=always_fail):
            failed = self.service.start_run('give-up', {'question': 'q'})
        self.assertEqual(failed['status'], 'error')
        self.assertEqual(failed['steps'][0]['attempt'], 2, 'retry_once 最多执行两次')
        self.assertEqual(failed['error'], 'still broken')

        def recovered(question, hits, **kwargs):
            return {'ok': True, 'text': 'ok'}

        with patch('app.workflows.service.agent_chat.answer', side_effect=recovered):
            retried = self.service.retry_run(failed['id'])
        self.assertEqual(retried['status'], 'success')
        self.assertEqual(retried['steps'][0]['attempt'], 3)
        self.assertEqual(retried['error'], '')

    def test_skip_policy_continues_with_next_step(self):
        self.flow('skipping', [
            {'key': 'first', 'kind': 'agent', 'agent_key': 'agent-a', 'on_error': 'skip'},
            {'key': 'second', 'kind': 'agent', 'agent_key': 'agent-b'},
        ])
        calls = []

        def answer(question, hits, **kwargs):
            calls.append(question)
            if len(calls) == 1:
                raise RuntimeError('optional step failed')
            return {'ok': True, 'text': 'next done'}

        with patch('app.workflows.service.agent_chat.answer', side_effect=answer):
            run = self.service.start_run('skipping', {'question': 'q'})

        self.assertEqual(run['status'], 'success')
        self.assertEqual(run['step_statuses'], ['skipped', 'success'])
        self.assertIn('optional step failed', run['error'])
        self.assertEqual(run['executed_step_keys'], ['second'])
        self.assertEqual(len(calls), 2, 'skip 后必须继续执行下一步')

    def test_approval_waits_then_approve_resumes_remaining_steps(self):
        self.flow('gated', [
            {'key': 'first', 'kind': 'agent', 'agent_key': 'agent-a'},
            {'key': 'gate', 'kind': 'approval', 'name': '确认'},
            {'key': 'third', 'kind': 'agent', 'agent_key': 'agent-b'},
        ])
        calls = []

        def answer(question, hits, **kwargs):
            calls.append(question)
            return {'ok': True, 'text': 'done'}

        with patch('app.workflows.service.agent_chat.answer', side_effect=answer):
            waiting = self.service.start_run_async('gated', {'question': 'q'})
            self.assertEqual(waiting['status'], 'waiting_approval')
            self.assertEqual(waiting['step_statuses'],
                             ['success', 'waiting_approval', 'pending'])
            self.assertIsNotNone(waiting['current_step_id'])
            self.assertEqual(len(calls), 1, '审批前只应执行第一步')
            resumed = self.service.approve_run_async(waiting['id'], 'confirmed')

        self.assertEqual(resumed['status'], 'success')
        self.assertEqual(resumed['step_statuses'], ['success', 'success', 'success'])
        self.assertIsNone(resumed['current_step_id'])
        self.assertEqual(len(calls), 2, '通过审批后应继续执行后续步骤')

    def test_rejecting_approval_cancels_run_without_later_steps(self):
        self.flow('rejected', [
            {'key': 'first', 'kind': 'agent', 'agent_key': 'agent-a'},
            {'key': 'gate', 'kind': 'approval', 'name': '确认'},
            {'key': 'third', 'kind': 'agent', 'agent_key': 'agent-b'},
        ])
        calls = []

        def answer(question, hits, **kwargs):
            calls.append(question)
            return {'ok': True, 'text': 'done'}

        with patch('app.workflows.service.agent_chat.answer', side_effect=answer):
            waiting = self.service.start_run_async('rejected', {'question': 'q'})
            rejected = self.service.approve_run_async(waiting['id'], 'rejected')

        self.assertEqual(rejected['status'], 'cancelled')
        self.assertTrue(rejected['finished_at'])
        self.assertEqual(len(calls), 1, '拒绝审批后不应执行后续步骤')

    def test_export_config_contains_definition_only_not_run_context(self):
        self.flow('exported', [
            {'key': 'first', 'kind': 'agent', 'agent_key': 'agent-a',
             'config': {'tier': 'fast'},
             'input_map': {'question': '$.input.question'}},
        ])
        input_sentinel = 'USER-PRIVATE-INPUT-SENTINEL'
        output_sentinel = 'AGENT-PRIVATE-OUTPUT-SENTINEL'

        def answer(question, hits, **kwargs):
            return {'ok': True, 'text': output_sentinel}

        with patch('app.workflows.service.agent_chat.answer', side_effect=answer):
            run = self.service.start_run('exported', {'question': input_sentinel})
        self.assertEqual(run['status'], 'success')
        self.assertIn(output_sentinel, json.dumps(run['output'], ensure_ascii=False))
        self.assertIn(input_sentinel, json.dumps(run['input'], ensure_ascii=False))

        exported = self.service.export_config('exported')
        text = json.dumps(exported, ensure_ascii=False)
        self.assertEqual(set(exported), {
            'key', 'name', 'workspace_key', 'description', 'trigger',
            'status', 'version', 'steps'})
        self.assertEqual(set(exported['steps'][0]), {
            'key', 'position', 'kind', 'name', 'agent_key', 'skill_key',
            'config', 'input_map', 'output_schema', 'on_error'})
        for leaked in (input_sentinel, output_sentinel):
            self.assertNotIn(leaked, text)
        for key in ('run_id', 'context', 'output', 'error'):
            self.assertNotIn('"%s"' % key, text)

    def test_background_start_returns_immediately_and_polls_progress(self):
        self.flow('polled', [
            {'key': 'first', 'kind': 'agent', 'agent_key': 'agent-a'},
        ])
        release = Event()
        started = Event()

        def slow(question, hits, **kwargs):
            started.set()
            release.wait(5)
            return {'ok': True, 'text': 'done'}

        with patch('app.workflows.service.agent_chat.answer', side_effect=slow):
            begin = time.monotonic()
            run = self.service.start_run_async('polled', {'question': 'q'},
                                               wait_seconds=0)
            elapsed = time.monotonic() - begin
            self.assertLess(elapsed, 2.0, '后台启动应立即返回')
            self.assertEqual(run['status'], 'running')
            self.assertEqual(run['error'], '')
            running = wait_until(lambda: (lambda item: item if item['steps'][0]['status'] == 'running'
                                          else None)(self.service.get_run(run['id'])))
            self.assertIsNotNone(running['current_step_id'], '轮询应能看到当前步骤')
            self.assertEqual(running['steps'][0]['finished_at'], '')
            release.set()
            done = wait_until(lambda: (lambda item: item if item['status'] == 'success'
                                       else None)(self.service.get_run(run['id'])))
        self.assertTrue(done['finished_at'])
        self.assertTrue(done['steps'][0]['finished_at'])
        self.assertIsNone(done['current_step_id'])


if __name__ == '__main__':
    unittest.main()
