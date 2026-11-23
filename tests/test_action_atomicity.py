"""T1.4/O-REL-2: an approval runs its local write at most once per lease."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.memory import db


class ApprovalLeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [
            patch.object(db, 'APP_DIR', root / 'app'),
            patch.object(db, 'DB_PATH', root / 'app' / 'actions.db'),
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

    def _propose(self, kind='update_mastery', target_type='kp', target_id='42'):
        return self.service.propose(
            workspace_key='studypal', run_id=self.run['id'], action_kind=kind,
            target_type=target_type, target_id=target_id,
            proposal={'mastery': '会用'}, before={'mastery': '读过'})

    def _row(self, action_id):
        conn = db.connect()
        try:
            return dict(conn.execute('SELECT * FROM agent_actions WHERE id=?', (action_id,)).fetchone())
        finally:
            conn.close()

    def _set_lease(self, action_id, modifier):
        """modifier is a raw SQLite date modifier such as '-11 minutes'."""
        conn = db.connect()
        try:
            with conn:
                conn.execute(
                    """UPDATE agent_actions SET status='approved',
                           approved_at=datetime('now','localtime'), attempt=1,
                           lease_expires_at=datetime('now','localtime',?)
                       WHERE id=?""", (modifier, action_id))
        finally:
            conn.close()

    def _set_failure(self, action_id, error):
        conn = db.connect()
        try:
            with conn:
                conn.execute(
                    """UPDATE agent_actions SET status='failed', error=?, attempt=1,
                           applied_at='', lease_expires_at=NULL
                       WHERE id=?""", (error, action_id))
        finally:
            conn.close()

    def test_approve_records_lease_then_clears_it_and_runs_once(self):
        action = self._propose()
        calls = []
        applied = self.service.approve(
            action['id'], workspace_key='studypal',
            executor=lambda payload: calls.append(payload))
        self.assertEqual(applied['status'], 'applied')
        self.assertEqual(len(calls), 1)
        # The executor receives dispatch metadata even though the proposal JSON
        # only carries the change itself.
        self.assertEqual(calls[0]['action_kind'], 'update_mastery')
        self.assertEqual(calls[0]['target_id'], '42')
        row = self._row(action['id'])
        self.assertEqual(row['attempt'], 1)
        self.assertTrue(row['approved_at'])
        self.assertIsNone(row['lease_expires_at'])
        repeated = self.service.approve(
            action['id'], workspace_key='studypal',
            executor=lambda payload: calls.append(payload))
        self.assertEqual(repeated['status'], 'applied')
        self.assertEqual(len(calls), 1)

    def test_live_lease_returns_without_second_execution(self):
        action = self._propose('create_event', 'event', '7')
        entered, release = threading.Event(), threading.Event()
        first_calls = []

        def first_executor(payload):
            first_calls.append(payload)
            entered.set()
            self.assertTrue(release.wait(10), 'executor release timed out')

        result = {}

        def run_first():
            try:
                result['value'] = self.service.approve(
                    action['id'], workspace_key='studypal', executor=first_executor)
            except BaseException as exc:  # pragma: no cover - surfacing in test
                result['error'] = exc

        thread = threading.Thread(target=run_first, daemon=True)
        thread.start()
        try:
            self.assertTrue(entered.wait(10), 'first executor never started')
            second_calls = []
            second = self.service.approve(
                action['id'], workspace_key='studypal',
                executor=lambda payload: second_calls.append(payload))
            self.assertEqual(second['status'], 'approved')
            self.assertEqual(second_calls, [])
        finally:
            release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertNotIn('error', result)
        self.assertEqual(result['value']['status'], 'applied')
        self.assertEqual(len(first_calls), 1)
        self.assertEqual(self._row(action['id'])['status'], 'applied')

    def test_explicit_executor_failure_is_terminal_and_clears_lease(self):
        from app.agent_runtime.actions import UNRETRYABLE_FAILURE_ERROR

        for outcome, message in (
            ({'ok': False, 'error': 'local write refused'}, 'local write refused'),
            ({'ok': False}, None),
            ({'ok': False, 'error': ''}, None),
        ):
            with self.subTest(outcome=outcome):
                action = self._propose()
                calls = []

                def execute(payload):
                    calls.append(payload)
                    return outcome

                result = self.service.approve(
                    action['id'], workspace_key='studypal', executor=execute)
                self.assertEqual(result['status'], 'failed')
                self.assertTrue(result['error'])
                if message:
                    self.assertEqual(result['error'], message)
                self.assertEqual(result['attempt'], 1)
                self.assertTrue(result['approved_at'])
                self.assertEqual(result['applied_at'], '')
                self.assertIsNone(result['lease_expires_at'])
                with self.assertRaisesRegex(ValueError, UNRETRYABLE_FAILURE_ERROR):
                    self.service.approve(
                        action['id'], workspace_key='studypal', executor=execute)
                self.assertEqual(len(calls), 1)
                self.assertEqual(self._row(action['id']), result)

    def test_legacy_executor_results_and_unchanged_success_remain_applied(self):
        conn = db.connect()
        try:
            raw_row = conn.execute("SELECT 42 AS id, 'raw row' AS title").fetchone()
        finally:
            conn.close()
        for outcome in (None, raw_row, {'id': 42, 'title': 'raw dict'},
                        {'ok': True, 'changed': False}, {'error': 'historical note'}):
            with self.subTest(outcome=outcome):
                action = self._propose()
                result = self.service.approve(
                    action['id'], workspace_key='studypal',
                    executor=lambda payload: outcome)
                self.assertEqual(result['status'], 'applied')
                self.assertEqual(result['error'], '')
                self.assertTrue(result['applied_at'])
                self.assertIsNone(result['lease_expires_at'])

    def test_returned_result_from_superseded_attempt_is_fenced(self):
        for older, newer, expected in (
            ({'ok': False, 'error': 'old failure'}, None, 'applied'),
            (None, {'ok': False, 'error': 'new failure'}, 'failed'),
        ):
            with self.subTest(expected=expected):
                action = self._propose()
                entered, release = threading.Event(), threading.Event()
                result = {}

                def slow_executor(payload):
                    entered.set()
                    self.assertTrue(release.wait(10), 'executor release timed out')
                    return older

                def first_approval():
                    try:
                        result['value'] = self.service.approve(
                            action['id'], workspace_key='studypal', executor=slow_executor)
                    except BaseException as exc:  # pragma: no cover - surfaced below
                        result['error'] = exc

                thread = threading.Thread(target=first_approval, daemon=True)
                thread.start()
                try:
                    self.assertTrue(entered.wait(10), 'executor never started')
                    self._set_lease(action['id'], '-11 minutes')
                    with self.assertLogs('app.agent_runtime.actions', level='WARNING'):
                        latest = self.service.approve(
                            action['id'], workspace_key='studypal',
                            executor=lambda payload: newer)
                        release.set()
                        thread.join(10)
                    self.assertFalse(thread.is_alive())
                finally:
                    release.set()
                    thread.join(10)
                self.assertNotIn('error', result)
                self.assertEqual(latest['status'], expected)
                self.assertEqual(latest['attempt'], 2)
                self.assertEqual(result['value'], latest)
                self.assertEqual(self._row(action['id']), latest)

    def test_recover_stale_actions_fails_expired_lease_only(self):
        from app.agent_runtime.actions import STALE_LEASE_ERROR
        from app.maintenance.recovery import recover_stale_actions

        stale = self._propose('create_event', 'event', '8')
        live = self._propose('create_event', 'event', '9')
        self._set_lease(stale['id'], '-11 minutes')
        self._set_lease(live['id'], '+30 minutes')
        recovered = recover_stale_actions()
        self.assertEqual([item['id'] for item in recovered], [stale['id']])
        self.assertEqual(recovered[0]['status'], 'failed')
        self.assertEqual(recovered[0]['error'], STALE_LEASE_ERROR)
        self.assertEqual(self._row(live['id'])['status'], 'approved')
        self.assertEqual(self._row(stale['id'])['status'], 'failed')

    def test_expired_lease_allows_a_retry_execution(self):
        action = self._propose()
        self._set_lease(action['id'], '-11 minutes')
        calls = []
        result = self.service.approve(
            action['id'], workspace_key='studypal',
            executor=lambda payload: calls.append(payload))
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(len(calls), 1)
        row = self._row(action['id'])
        self.assertEqual(row['attempt'], 2)
        self.assertTrue(row['applied_at'])
        self.assertIsNone(row['lease_expires_at'])

    def test_stale_lease_failure_is_retryable(self):
        from app.agent_runtime.actions import STALE_LEASE_ERROR

        action = self._propose()
        self._set_failure(action['id'], STALE_LEASE_ERROR)
        calls = []
        result = self.service.approve(
            action['id'], workspace_key='studypal',
            executor=lambda payload: calls.append(payload))
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(result['attempt'], 2)
        self.assertEqual(result['error'], '')
        self.assertTrue(result['applied_at'])
        self.assertEqual(len(calls), 1)

    def test_non_stale_failure_is_terminal(self):
        from app.agent_runtime.actions import UNRETRYABLE_FAILURE_ERROR

        action = self._propose()
        self._set_failure(action['id'], '动作执行失败：磁盘已满')
        calls = []
        with self.assertRaises(ValueError) as error:
            self.service.approve(action['id'], workspace_key='studypal',
                                 executor=lambda payload: calls.append(payload))
        self.assertEqual(str(error.exception), UNRETRYABLE_FAILURE_ERROR)
        self.assertEqual(calls, [])
        row = self._row(action['id'])
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['error'], '动作执行失败：磁盘已满')
        self.assertEqual(row['attempt'], 1)

    def test_superseded_attempt_cannot_overwrite_newer_result(self):
        """attempt=1 finishes after attempt=2 applied: its result is discarded."""
        action = self._propose('create_event', 'event', '11')
        started, release = threading.Event(), threading.Event()
        first_result = {}

        def slow_executor(payload):
            started.set()
            self.assertTrue(release.wait(10), 'first executor release timed out')
            raise RuntimeError('attempt 1 finished too late')

        def run_first():
            try:
                first_result['value'] = self.service.approve(
                    action['id'], workspace_key='studypal', executor=slow_executor)
            except BaseException as exc:  # pragma: no cover - surfaced in assertion
                first_result['error'] = exc

        thread = threading.Thread(target=run_first, daemon=True)
        thread.start()
        try:
            self.assertTrue(started.wait(10), 'first executor never started')
            # attempt=1's lease expires while its executor is still running.
            self._set_lease(action['id'], '-11 minutes')
            second_calls = []
            with self.assertLogs('app.agent_runtime.actions', level='WARNING') as captured:
                second = self.service.approve(
                    action['id'], workspace_key='studypal',
                    executor=lambda payload: second_calls.append(payload))
                self.assertEqual(second['status'], 'applied')
                self.assertEqual(second['attempt'], 2)
                release.set()
                thread.join(10)
            self.assertFalse(thread.is_alive())
        finally:
            release.set()
            thread.join(10)
        self.assertNotIn('error', first_result)
        # The stale finish neither overwrote the newer attempt nor changed status.
        self.assertEqual(first_result['value']['status'], 'applied')
        self.assertEqual(first_result['value']['attempt'], 2)
        self.assertEqual(self._row(action['id'])['status'], 'applied')
        self.assertEqual(self._row(action['id'])['attempt'], 2)
        self.assertEqual(len(second_calls), 1)
        self.assertTrue(any('stale attempt finish discarded' in line
                            for line in captured.output))

    def test_recover_stale_fences_late_finish_from_crashed_execution(self):
        """recover_stale bumps attempt, so the crashed process's finish is discarded."""
        from app.agent_runtime.actions import STALE_LEASE_ERROR

        action = self._propose('create_event', 'event', '12')
        started, release = threading.Event(), threading.Event()
        first_result = {}

        def slow_executor(payload):
            started.set()
            self.assertTrue(release.wait(10), 'executor release timed out')

        def run_first():
            try:
                first_result['value'] = self.service.approve(
                    action['id'], workspace_key='studypal', executor=slow_executor)
            except BaseException as exc:  # pragma: no cover - surfaced in assertion
                first_result['error'] = exc

        thread = threading.Thread(target=run_first, daemon=True)
        thread.start()
        try:
            self.assertTrue(started.wait(10), 'first executor never started')
            # The executor is still running while startup recovery declares the lease stale.
            self._set_lease(action['id'], '-11 minutes')
            recovered = self.service.recover_stale()
            self.assertEqual([item['id'] for item in recovered], [action['id']])
            self.assertEqual(recovered[0]['status'], 'failed')
            self.assertEqual(recovered[0]['error'], STALE_LEASE_ERROR)
            self.assertEqual(recovered[0]['attempt'], 2)
            with self.assertLogs('app.agent_runtime.actions', level='WARNING') as captured:
                release.set()
                thread.join(10)
            self.assertFalse(thread.is_alive())
        finally:
            release.set()
            thread.join(10)
        self.assertNotIn('error', first_result)
        # The late (successful) finish from attempt=1 is fenced out.
        self.assertEqual(first_result['value']['status'], 'failed')
        self.assertEqual(first_result['value']['attempt'], 2)
        row = self._row(action['id'])
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['error'], STALE_LEASE_ERROR)
        self.assertEqual(row['attempt'], 2)
        self.assertEqual(row['applied_at'], '')
        self.assertTrue(any('stale attempt finish discarded' in line
                            for line in captured.output))

    def test_approve_rejects_unknown_or_unapprovable_actions(self):
        with self.assertRaises(ValueError):
            self.service.approve(10 ** 9, workspace_key='studypal', executor=lambda payload: None)
        action = self._propose()
        self.service.reject(action['id'], workspace_key='studypal')
        with self.assertRaises(ValueError):
            self.service.approve(action['id'], workspace_key='studypal',
                                 executor=lambda payload: None)


if __name__ == '__main__':
    unittest.main()
