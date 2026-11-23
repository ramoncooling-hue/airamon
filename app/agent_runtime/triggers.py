"""Small, local trigger scanner for the Personal Agent runtime.

The scanner creates bounded run records and, where a write is involved,
proposes an action. It never applies a business change by itself.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ..memory import db, repo, sources
from .actions import ActionService
from .service import PersonalAgentService


def _date(now: datetime) -> str:
    return now.date().isoformat()


def _create_run(
    runtime: PersonalAgentService,
    *,
    workspace: str,
    kind: str,
    target_id: str,
    now: datetime,
    summary: str,
    input_data: dict[str, Any] | None = None,
    waiting: bool = False,
) -> dict[str, Any]:
    run = runtime.create_run(
        workspace_key=workspace,
        trigger_type=kind,
        trigger_id=target_id,
        input_data=input_data or {},
        dedupe_key=f'{workspace}:{kind}:{target_id}:{_date(now)}',
    )
    if run.get('status') == 'queued':
        runtime.update_run(
            run['id'],
            status='waiting_approval' if waiting else 'succeeded',
            summary=summary,
        )
    return runtime.get_run(run['id'], workspace_key=workspace)


def _pending_action_exists(workspace: str, action_kind: str, target_id: str) -> bool:
    conn = db.connect()
    try:
        row = conn.execute(
            """SELECT 1 FROM agent_actions
               WHERE workspace_key=? AND action_kind=? AND target_id=? AND status='proposed'""",
            (workspace, action_kind, target_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _scan_sources(runtime: PersonalAgentService, actions: ActionService, workspace: dict, now: datetime) -> int:
    if workspace['key'] != 'studypal':
        return 0
    created = 0
    courses = repo.list_courses(workspace['key'])
    for course in courses:
        for candidate in sources.candidates(course['name']):
            if candidate.get('status') != 'pending':
                continue
            target_id = str(candidate['id'])
            run = _create_run(
                runtime,
                workspace=workspace['key'],
                kind='source_arrived',
                target_id=target_id,
                now=now,
                summary=f"{course['name']} 有待确认知识候选：{candidate['label']}",
                input_data={'course': course['name'], 'candidate_id': candidate['id']},
                waiting=True,
            )
            if not _pending_action_exists(workspace['key'], 'confirm_source_candidate', target_id):
                actions.propose(
                    workspace_key=workspace['key'],
                    run_id=run['id'],
                    action_kind='confirm_source_candidate',
                    target_type='knowledge_candidate',
                    target_id=target_id,
                    proposal={
                        'course': course['name'],
                        'candidate_id': candidate['id'],
                        'title': candidate['label'],
                    },
                )
                created += 1
    return created


def _scan_events(runtime: PersonalAgentService, workspace: dict, now: datetime) -> int:
    created = 0
    today = now.date()
    horizon = today + timedelta(days=14)
    for event in repo.list_events(workspace=workspace['key']):
        try:
            event_date = datetime.strptime(str(event.get('date', ''))[:10], '%Y-%m-%d').date()
        except ValueError:
            continue
        if not today <= event_date <= horizon:
            continue
        run = _create_run(
            runtime,
            workspace=workspace['key'],
            kind='event_due',
            target_id=str(event.get('id') or event.get('item_id') or event.get('title')),
            now=now,
            summary=f"{event_date.isoformat()} · {event.get('title', '待办事项')}",
            input_data={'event_id': event.get('id'), 'date': event_date.isoformat()},
        )
        created += int(run.get('status') == 'succeeded')
    return created


def _scan_workflows(runtime: PersonalAgentService, workspace: dict, now: datetime) -> int:
    conn = db.connect()
    try:
        rows = conn.execute(
            """SELECT wr.id, wr.current_step_id, w.key, w.name
               FROM workflow_runs wr JOIN workflows w ON w.id=wr.workflow_id
               WHERE wr.status='waiting_approval' AND w.workspace_key=?""",
            (workspace['key'],),
        ).fetchall()
    finally:
        conn.close()
    created = 0
    for row in rows:
        run = _create_run(
            runtime,
            workspace=workspace['key'],
            kind='workflow_waiting',
            target_id=str(row['id']),
            now=now,
            summary=f"工作流「{row['name']}」等待人工确认",
            input_data={'workflow_key': row['key'], 'workflow_run_id': row['id'], 'step_id': row['current_step_id']},
            waiting=True,
        )
        created += int(run.get('status') == 'waiting_approval')
    return created


def _scan_stale_goals(runtime: PersonalAgentService, workspace: dict, now: datetime) -> int:
    created = 0
    cutoff = now - timedelta(days=7)
    for goal in runtime.list_goals(workspace_key=workspace['key'], status='active'):
        try:
            updated = datetime.fromisoformat(str(goal.get('updated_at', '')).replace(' ', 'T'))
        except ValueError:
            continue
        if updated > cutoff:
            continue
        run = _create_run(
            runtime,
            workspace=workspace['key'],
            kind='goal_stale',
            target_id=str(goal['id']),
            now=now,
            summary=f"目标「{goal['title']}」已有 7 天没有推进",
            input_data={'goal_id': goal['id']},
        )
        created += int(run.get('status') == 'succeeded')
    return created


def run_once(now: datetime | None = None, *, runtime: PersonalAgentService | None = None) -> dict[str, int]:
    """Run one deterministic scan; safe to call from tests or the local loop."""
    current = now or datetime.now()
    runtime = runtime or PersonalAgentService()
    actions = ActionService()
    result = {'source_arrived': 0, 'event_due': 0, 'workflow_waiting': 0, 'goal_stale': 0, 'errors': 0}
    for workspace in repo.list_workspaces():
        try:
            result['source_arrived'] += _scan_sources(runtime, actions, workspace, current)
            result['event_due'] += _scan_events(runtime, workspace, current)
            result['workflow_waiting'] += _scan_workflows(runtime, workspace, current)
            result['goal_stale'] += _scan_stale_goals(runtime, workspace, current)
        except Exception:
            result['errors'] += 1
    return result


__all__ = ['run_once']
