"""Startup recovery for durable background state."""

from __future__ import annotations

from ..agent_runtime.actions import ActionService
from ..memory import db


def recover_stale_workflow_runs() -> list[dict]:
    """Fail workflow runs whose background worker died with the previous process.

    workflow_runs has no 'interrupted' status in the frozen 017 schema, so a run
    that was still 'running' at startup becomes 'error' with a retryable
    explanation (retry_run accepts error runs). waiting_approval is durable user
    intent and is left untouched. Safe to call repeatedly.
    """
    conn = db.connect()
    try:
        with conn:
            stale = [dict(row) for row in conn.execute(
                "SELECT id,workflow_id FROM workflow_runs WHERE status='running' ORDER BY id").fetchall()]
            if not stale:
                return []
            conn.execute("""UPDATE workflow_run_steps
                               SET status='error',
                                   error=CASE WHEN error='' THEN '上次运行中断，请重试' ELSE error END,
                                   finished_at=datetime('now','localtime')
                             WHERE status IN ('pending','running')
                               AND run_id IN (SELECT id FROM workflow_runs WHERE status='running')""")
            conn.execute("""UPDATE workflow_runs
                               SET status='error',
                                   error='上次运行中断（服务重启），请重试',
                                   finished_at=datetime('now','localtime'),
                                   duration_ms=MAX(0, CAST(
                                       (julianday('now','localtime')-julianday(started_at))*86400000 AS INTEGER))
                             WHERE status='running'""")
        return stale
    finally:
        conn.close()


def recover_stale_actions() -> list[dict]:
    """Fail actions whose execution lease expired before this process started.

    Call once after db.apply_schema() and before serving requests. Safe to run
    repeatedly: only rows still marked approved with an expired lease change.
    Workflow runs are recovered through the same startup hook so no request
    thread ever finds a permanently running workflow.
    """
    actions = ActionService().recover_stale()
    try:
        recover_stale_workflow_runs()
    except Exception:
        # Agent action recovery must not be blocked by workflow history repair.
        pass
    return actions
