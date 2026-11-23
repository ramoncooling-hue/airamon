import json
import threading
import time
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import patch

from app.hub import registry
from app.maintenance import recovery
from app.memory import db
from app.workflows import WorkflowService
from app.workflows import service as workflow_service


def wait_until(predicate, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError('condition was not met in time')


class WorkflowBackgroundTests(unittest.TestCase):
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
        self.add_hub_entry('agent', 'agent-a')
        self.add_hub_entry('agent', 'agent-b')
        self.service = WorkflowService()

    def add_hub_entry(self, kind, key):
        directory = (self.skills if kind == 'skill' else self.agents) / key
        directory.mkdir()
        content_name = 'SKILL.md' if kind == 'skill' else 'system.md'
        (directory / content_name).write_text(f'# {key}\n', encoding='utf-8')
        (directory / 'manifest.json').write_text(json.dumps({
            'key': key, 'name': key, 'version': '1.0.0', 'skills': [],
        }), encoding='utf-8')
        registry.sync()

    def make_agent_flow(self, key='flow', after=None):
        self.service.create({'key': key, 'name': key})
        self.service.add_step(key, {'key': 'first', 'kind': 'agent', 'agent_key': 'agent-a'})
        if after == 'approval':
            self.service.add_step(key, {'key': 'second', 'kind': 'approval', 'name': '确认'})
        elif after is not None:
            self.service.add_step(key, {'key': 'second', 'kind': after, 'agent_key': 'agent-b'})
        return key

    def test_start_returns_while_slow_step_still_runs_and_then_completes(self):
        self.make_agent_flow('slow')
        release = Event()

        def slow_answer(question, hits, **kwargs):
            release.wait(5)
            return {'ok': True, 'text': 'done'}

        with patch('app.workflows.service.agent_chat.answer', side_effect=slow_answer):
            started_at = time.monotonic()
            run = self.service.start_run_async('slow', {'question': 'q'})
            elapsed = time.monotonic() - started_at
            self.assertLess(elapsed, 2.0, '启动请求不应等待慢步骤结束')
            self.assertEqual(run['status'], 'running')
            # 轮询可见进度：步骤已被后台线程认领。
            running = self.service.get_run(run['id'])
            self.assertEqual(running['steps'][0]['status'], 'running')
            release.set()
            done = wait_until(lambda: (lambda item: item if item['status'] == 'success' else None)(
                self.service.get_run(run['id'])))
        self.assertEqual(done['status'], 'success')
        self.assertEqual(done['step_statuses'], ['success'])

    def test_approval_gate_returns_and_async_approve_resumes_in_background(self):
        self.make_agent_flow('gated', after='approval')
        calls = []

        def answer(question, hits, **kwargs):
            calls.append(question)
            return {'ok': True, 'text': 'done'}

        with patch('app.workflows.service.agent_chat.answer', side_effect=answer):
            first = self.service.start_run_async('gated', {'question': 'q'})
            self.assertEqual(first['status'], 'waiting_approval')
            self.assertEqual(first['step_statuses'], ['success', 'waiting_approval'])
            resumed = self.service.approve_run_async(first['id'], 'confirmed')
        self.assertEqual(resumed['status'], 'success')
        self.assertEqual(len(calls), 1)

    def test_cancel_is_honoured_by_the_background_worker(self):
        self.make_agent_flow('cancelable')
        started = Event()
        release = Event()

        def slow_answer(question, hits, **kwargs):
            started.set()
            release.wait(5)
            return {'ok': True, 'text': 'done'}

        with patch('app.workflows.service.agent_chat.answer', side_effect=slow_answer):
            run = self.service.start_run_async('cancelable', {'question': 'q'})
            self.assertEqual(run['status'], 'running')
            self.assertTrue(started.wait(3))
            cancelled = self.service.cancel_run(run['id'])
            self.assertEqual(cancelled['status'], 'cancelled')
            release.set()
            # 后台线程在取消后停止写回，运行保持 cancelled。
            wait_until(lambda: self.service.get_run(run['id'])['steps'][0]['status'] != 'running')
            final = self.service.get_run(run['id'])
        self.assertEqual(final['status'], 'cancelled')

    def test_startup_recovery_fails_orphaned_running_run_but_keeps_approval_waiting(self):
        self.service.create({'key': 'recover', 'name': 'recover'})
        self.service.add_step('recover', {'key': 'first', 'kind': 'agent', 'agent_key': 'agent-a'})
        conn = db.connect()
        try:
            workflow_id = conn.execute(
                "SELECT id FROM workflows WHERE key='recover'").fetchone()[0]
            step_id = conn.execute(
                'SELECT id FROM workflow_steps WHERE workflow_id=?', (workflow_id,)).fetchone()[0]
            orphan_id = conn.execute(
                "INSERT INTO workflow_runs(workflow_id,status) VALUES(?,'running')", (workflow_id,)).lastrowid
            conn.execute("INSERT INTO workflow_run_steps(run_id,step_id,status) VALUES(?,?, 'running')",
                         (orphan_id, step_id))
            waiting_id = conn.execute(
                "INSERT INTO workflow_runs(workflow_id,status) VALUES(?,'waiting_approval')", (workflow_id,)).lastrowid
            conn.execute("INSERT INTO workflow_run_steps(run_id,step_id,status) VALUES(?,?, 'waiting_approval')",
                         (waiting_id, step_id))
            conn.commit()
        finally:
            conn.close()

        # 启动 hook（recover_stale_actions）复用 recovery 范式，无需额外接入点。
        recovery.recover_stale_actions()
        orphan = self.service.get_run(orphan_id)
        self.assertEqual(orphan['status'], 'error')
        self.assertIn('中断', orphan['error'])
        self.assertEqual(orphan['steps'][0]['status'], 'error')
        self.assertTrue(orphan['finished_at'])
        self.assertEqual(self.service.get_run(waiting_id)['status'], 'waiting_approval')

    def test_concurrent_starts_execute_each_run_only_once(self):
        self.make_agent_flow('parallel')
        calls = []
        lock = threading.Lock()

        def answer(question, hits, **kwargs):
            with lock:
                calls.append(question)
            time.sleep(0.05)
            return {'ok': True, 'text': 'done'}

        with patch('app.workflows.service.agent_chat.answer', side_effect=answer):
            with ThreadPoolExecutor(max_workers=2) as pool:
                runs = list(pool.map(lambda _: self.service.start_run_async(
                    'parallel', {'question': 'q'}, wait_seconds=0), range(2)))
            for run in runs:
                wait_until(lambda run_id=run['id']: (lambda item: item if item['status'] == 'success' else None)(
                    self.service.get_run(run_id)))
        self.assertEqual(len({run['id'] for run in runs}), 2)
        self.assertEqual(len(calls), 2, '每个 run 只应执行一次')


if __name__ == '__main__':
    unittest.main()
