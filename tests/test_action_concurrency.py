"""B1.1/P2-5: 线程级并发 approve 只执行一次；过期租约允许重跑（attempt+1）。"""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.memory import db


class ApprovalConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [
            patch.object(db, 'APP_DIR', root / 'app'),
            patch.object(db, 'DB_PATH', root / 'app' / 'concurrency.db'),
        ]
        for item in self.patches:
            item.start()
        db.apply_schema()
        from app.agent_runtime.actions import ActionService
        from app.agent_runtime.service import PersonalAgentService
        self.run = PersonalAgentService().create_run(workspace_key='studypal')
        self.service = ActionService()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def _propose(self):
        return self.service.propose(
            workspace_key='studypal', run_id=self.run['id'],
            action_kind='update_mastery', target_type='kp', target_id='42',
            proposal={'mastery': '会用'}, before={'mastery': '读过'})

    def _row(self, action_id):
        conn = db.connect()
        try:
            return dict(conn.execute(
                'SELECT * FROM agent_actions WHERE id=?', (action_id,)).fetchone())
        finally:
            conn.close()

    def _set_expired_lease(self, action_id):
        """模拟上次进程在 claim 后崩溃、租约已过期。"""
        conn = db.connect()
        try:
            with conn:
                conn.execute(
                    """UPDATE agent_actions
                       SET status='approved', approved_at=datetime('now','localtime'),
                           attempt=1,
                           lease_expires_at=datetime('now','localtime','-11 minutes')
                       WHERE id=?""", (action_id,))
        finally:
            conn.close()

    def test_two_threads_approve_same_action_execute_once(self):
        action = self._propose()
        calls = []
        executor_started = threading.Event()
        release_executor = threading.Event()

        def executor(payload):
            calls.append(payload)
            executor_started.set()
            # 占住执行阶段，让另一个线程必然看到 live lease 并直接返回。
            self.assertTrue(release_executor.wait(5), 'executor release timed out')

        results = {}
        barrier = threading.Barrier(2)

        def worker(name):
            barrier.wait(5)
            try:
                results[name] = self.service.approve(
                    action['id'], workspace_key='studypal', executor=executor)
            except BaseException as exc:  # 线程异常带回主线程断言
                results[name] = exc

        threads = [threading.Thread(target=worker, args=(name,), daemon=True)
                   for name in ('first', 'second')]
        for thread in threads:
            thread.start()
        try:
            self.assertTrue(executor_started.wait(5), 'executor never started')
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not results:
                time.sleep(0.01)
            self.assertTrue(results, 'lease holder did not return while executor ran')
        finally:
            release_executor.set()
            for thread in threads:
                thread.join(5)
        self.assertFalse([thread for thread in threads if thread.is_alive()],
                         'approve worker thread did not finish')

        for outcome in results.values():
            self.assertNotIsInstance(outcome, BaseException, outcome)
        # 一个线程执行，另一个看到 live lease 返回当前行（approved），不会双执行。
        self.assertEqual(sorted(result['status'] for result in results.values()),
                         ['applied', 'approved'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['action_kind'], 'update_mastery')
        row = self._row(action['id'])
        self.assertEqual(row['status'], 'applied')
        self.assertEqual(row['attempt'], 1)
        self.assertTrue(row['applied_at'])
        self.assertIsNone(row['lease_expires_at'])

    def test_expired_lease_allows_a_retry_execution(self):
        action = self._propose()
        self._set_expired_lease(action['id'])
        calls = []
        result = self.service.approve(
            action['id'], workspace_key='studypal',
            executor=lambda payload: calls.append(payload))
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(len(calls), 1)
        row = self._row(action['id'])
        self.assertEqual(row['attempt'], 2)
        self.assertEqual(row['status'], 'applied')
        self.assertTrue(row['applied_at'])
        self.assertIsNone(row['lease_expires_at'])


if __name__ == '__main__':
    unittest.main()
