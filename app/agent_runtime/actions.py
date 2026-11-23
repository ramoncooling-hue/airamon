"""Action proposals and approval state transitions."""

from __future__ import annotations

import json
import logging
from typing import Any

from ..memory import db
from .security import reject_secrets

ALLOWED_ACTIONS = {
    'save_conversation', 'save_web_source', 'create_workspace_item',
    'update_workspace_item', 'create_event', 'update_event',
    'create_workflow_run', 'confirm_source_candidate',
    'confirm_relation_candidate', 'update_mastery', 'save_memory_block',
}
MAX_ACTIONS_PER_RUN = 8
# T1.4/O-REL-2: an approved action owns a short execution lease. A process
# that dies mid-write leaves an expired lease; see recover_stale() below.
APPROVAL_LEASE_MINUTES = 10
STALE_LEASE_ERROR = '执行租约超时，未完成执行（可重试，重新确认即可）'
UNRETRYABLE_FAILURE_ERROR = '该动作执行失败且不可自动重试，请重新发起动作'
_LOGGER = logging.getLogger(__name__)


def _json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, separators=(',', ':'))


def _row(row) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _payload(row) -> dict[str, Any]:
    payload = dict(json.loads(row['proposal_json'] or '{}'))
    # Keep action metadata out of the user-facing proposal JSON while
    # giving the executor enough information to dispatch safely.
    payload.setdefault('action_kind', row['action_kind'])
    payload.setdefault('target_type', row['target_type'])
    payload.setdefault('target_id', row['target_id'])
    return payload


class ActionService:
    def __init__(self, connection_factory=db.connect):
        self._connection_factory = connection_factory

    def propose(
        self,
        *,
        workspace_key: str,
        run_id: int,
        action_kind: str,
        target_type: str = '',
        target_id: str = '',
        proposal: dict[str, Any] | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if action_kind not in ALLOWED_ACTIONS:
            raise ValueError(f'不支持的动作类型：{action_kind}')
        reject_secrets(proposal or {}, 'proposal')
        reject_secrets(before or {}, 'before')
        reject_secrets(after or {}, 'after')
        conn = self._connection_factory()
        try:
            run = conn.execute(
                'SELECT workspace_key FROM agent_runs WHERE id=?', (run_id,)
            ).fetchone()
            if run is None:
                raise ValueError(f'运行记录不存在：{run_id}')
            if run['workspace_key'] != workspace_key:
                raise ValueError('运行记录与动作工作区不一致')
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM agent_actions WHERE run_id=?",
                (run_id,),
            ).fetchone()['n']
            if count >= MAX_ACTIONS_PER_RUN:
                raise ValueError('单次运行最多只能提出 8 个动作')
            cur = conn.execute(
                """INSERT INTO agent_actions
                   (run_id, workspace_key, action_kind, target_type, target_id,
                    proposal_json, before_json, after_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, workspace_key, action_kind, target_type, target_id,
                 _json(proposal), _json(before), _json(after or proposal)),
            )
            conn.commit()
            return _row(conn.execute('SELECT * FROM agent_actions WHERE id=?', (cur.lastrowid,)).fetchone())
        finally:
            conn.close()

    def pending(self, *, workspace_key: str) -> list[dict[str, Any]]:
        conn = self._connection_factory()
        try:
            rows = conn.execute(
                """SELECT * FROM agent_actions
                   WHERE workspace_key=? AND status='proposed'
                   ORDER BY id DESC""",
                (workspace_key,),
            ).fetchall()
            return [_row(row) for row in rows]
        finally:
            conn.close()

    def reject(self, action_id: int, *, workspace_key: str) -> dict[str, Any]:
        conn = self._connection_factory()
        try:
            row = conn.execute(
                'SELECT * FROM agent_actions WHERE id=? AND workspace_key=?',
                (action_id, workspace_key),
            ).fetchone()
            if row is None:
                raise ValueError(f'动作不存在：{action_id}')
            if row['status'] != 'proposed':
                raise ValueError(f'动作不能拒绝：{row["status"]}')
            conn.execute(
                """UPDATE agent_actions
                   SET status='rejected', decided_at=datetime('now','localtime')
                   WHERE id=? AND workspace_key=?""",
                (action_id, workspace_key),
            )
            conn.commit()
            return _row(conn.execute('SELECT * FROM agent_actions WHERE id=?', (action_id,)).fetchone())
        finally:
            conn.close()

    def approve(self, action_id: int, *, workspace_key: str, executor) -> dict[str, Any]:
        """Approve and execute one proposal at most once per execution lease.

        The proposed -> approved transition is claimed with a short lease in a
        single write transaction (schema 021_action_leases.sql). A concurrent
        click or a retried HTTP request that sees a live lease returns the
        current row without running the local write twice. An expired lease
        means the previous process died mid-execution, so the row is
        re-claimed and executed again (at-least-once). recover_stale() turns
        such rows into a failed row carrying STALE_LEASE_ERROR, and approving
        that failure again is allowed; any other failed row stays terminal.
        The claimed attempt is passed into _finish() so a slow execution that
        was already superseded by a newer attempt cannot overwrite its result.
        """
        attempt = None
        payload = None
        conn = self._connection_factory()
        try:
            conn.execute('BEGIN IMMEDIATE')
            try:
                row = conn.execute(
                    'SELECT * FROM agent_actions WHERE id=? AND workspace_key=?',
                    (action_id, workspace_key),
                ).fetchone()
                if row is None:
                    raise ValueError(f'动作不存在：{action_id}')
                status = row['status']
                if status == 'applied':
                    # Approval is idempotent from the user's perspective. A
                    # double click or a retried HTTP request must not re-run a
                    # local write.
                    conn.commit()
                    return _row(row)
                if status == 'approved' and self._lease_live(conn, row):
                    # Another request currently owns the execution lease.
                    conn.commit()
                    return _row(row)
                if status == 'failed':
                    if row['error'] != STALE_LEASE_ERROR:
                        raise ValueError(UNRETRYABLE_FAILURE_ERROR)
                elif status not in {'proposed', 'approved'}:
                    raise ValueError(f'动作不能确认：{status}')
                attempt = self._claim(conn, action_id, workspace_key)
                payload = _payload(row)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        finally:
            conn.close()

        try:
            result = executor(payload)
        except Exception as exc:
            return self._finish(action_id, workspace_key, 'failed',
                                error=str(exc), attempt=attempt)
        if isinstance(result, dict) and result.get('ok') is False:
            return self._finish(
                action_id, workspace_key, 'failed',
                error=str(result.get('error') or '动作执行失败'), attempt=attempt)
        return self._finish(action_id, workspace_key, 'applied', attempt=attempt)

    def recover_stale(self) -> list[dict[str, Any]]:
        """Fail approvals whose execution lease expired without a result row.

        Startup recovery for a process that died between claiming an approval
        and recording its execution result. The row keeps STALE_LEASE_ERROR in
        the error column, which marks it as retryable: approving it again
        re-claims the lease (attempt + 1) and runs the executor once more. The
        attempt is bumped here as well, so a late _finish() from the process
        that owned the expired lease is fenced out and cannot flip this failed
        row back to applied.
        """
        conn = self._connection_factory()
        try:
            with conn:
                stale = conn.execute(
                    """SELECT * FROM agent_actions
                       WHERE status='approved' AND applied_at=''
                         AND (lease_expires_at IS NULL
                              OR lease_expires_at < datetime('now','localtime'))"""
                ).fetchall()
                if not stale:
                    return []
                conn.execute(
                    """UPDATE agent_actions
                       SET status='failed', error=?, lease_expires_at=NULL,
                           attempt=attempt+1
                       WHERE status='approved' AND applied_at=''
                         AND (lease_expires_at IS NULL
                              OR lease_expires_at < datetime('now','localtime'))""",
                    (STALE_LEASE_ERROR,),
                )
                ids = [row['id'] for row in stale]
                placeholders = ','.join('?' for _ in ids)
                rows = conn.execute(
                    f'SELECT * FROM agent_actions WHERE id IN ({placeholders}) ORDER BY id',
                    ids,
                ).fetchall()
            return [_row(row) for row in rows]
        finally:
            conn.close()

    @staticmethod
    def _lease_live(conn, row) -> bool:
        lease = row['lease_expires_at']
        if not lease:
            return False
        now = conn.execute("SELECT datetime('now','localtime') AS now").fetchone()['now']
        return str(lease) >= str(now)

    @staticmethod
    def _claim(conn, action_id: int, workspace_key: str) -> int:
        """Claim the lease and return the new attempt number in the same transaction."""
        conn.execute(
            f"""UPDATE agent_actions
                SET status='approved',
                    decided_at=datetime('now','localtime'),
                    approved_at=datetime('now','localtime'),
                    attempt=attempt+1,
                    lease_expires_at=datetime('now','localtime','+{APPROVAL_LEASE_MINUTES} minutes'),
                    error=''
                WHERE id=? AND workspace_key=?""",
            (action_id, workspace_key),
        )
        row = conn.execute(
            'SELECT attempt FROM agent_actions WHERE id=? AND workspace_key=?',
            (action_id, workspace_key),
        ).fetchone()
        return int(row['attempt']) if row is not None else 0

    def _finish(self, action_id: int, workspace_key: str, status: str, *,
                error: str = '', attempt: int | None = None) -> dict[str, Any]:
        """Record the executor outcome, fenced by attempt when provided.

        An execution whose lease expired and was re-claimed by a newer attempt
        must not overwrite that newer attempt's outcome: the fenced UPDATE
        matches no row, the stale result is logged and discarded, and the
        current row is returned instead.
        """
        conn = self._connection_factory()
        try:
            sql = """UPDATE agent_actions
                     SET status=?, error=?, lease_expires_at=NULL,
                         applied_at=CASE WHEN ?='applied' THEN datetime('now','localtime') ELSE applied_at END
                     WHERE id=? AND workspace_key=?"""
            params: list[Any] = [status, error, status, action_id, workspace_key]
            if attempt is not None:
                sql += ' AND attempt=?'
                params.append(attempt)
            cursor = conn.execute(sql, params)
            if attempt is not None and cursor.rowcount == 0:
                _LOGGER.warning(
                    'stale attempt finish discarded: action=%s workspace=%s attempt=%s status=%s',
                    action_id, workspace_key, attempt, status)
            conn.commit()
            return _row(conn.execute('SELECT * FROM agent_actions WHERE id=?', (action_id,)).fetchone())
        finally:
            conn.close()
