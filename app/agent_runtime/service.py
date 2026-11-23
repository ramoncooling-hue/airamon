"""Persistent Personal Agent run and goal primitives."""

from __future__ import annotations

import json
from typing import Any

from ..memory import db
from .security import reject_secrets


def _json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, separators=(',', ':'))


def _row(row) -> dict[str, Any]:
    return dict(row) if row is not None else {}


class PersonalAgentService:
    def __init__(self, connection_factory=db.connect):
        self._connection_factory = connection_factory

    def create_run(
        self,
        *,
        workspace_key: str,
        agent_key: str = 'laoba',
        trigger_type: str = 'manual',
        trigger_id: str = '',
        input_data: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        dedupe_key: str = '',
    ) -> dict[str, Any]:
        if not workspace_key.strip():
            raise ValueError('workspace_key 不能为空')
        reject_secrets(input_data or {}, 'input')
        reject_secrets(context or {}, 'context')
        conn = self._connection_factory()
        try:
            if dedupe_key:
                existing = conn.execute(
                    """SELECT * FROM agent_runs
                       WHERE workspace_key=? AND trigger_type=? AND dedupe_key=?""",
                    (workspace_key, trigger_type, dedupe_key),
                ).fetchone()
                if existing is not None:
                    return _row(existing)
            cur = conn.execute(
                """INSERT INTO agent_runs
                   (workspace_key, agent_key, trigger_type, trigger_id,
                    input_json, context_json, dedupe_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    workspace_key,
                    agent_key,
                    trigger_type,
                    trigger_id,
                    _json(input_data),
                    _json(context),
                    dedupe_key,
                ),
            )
            conn.commit()
            return _row(conn.execute('SELECT * FROM agent_runs WHERE id=?', (cur.lastrowid,)).fetchone())
        finally:
            conn.close()

    def get_profile(self) -> dict[str, Any]:
        conn = self._connection_factory()
        try:
            row = conn.execute('SELECT * FROM user_profile WHERE id=1').fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO user_profile
                       (id, display_name, preferences_json, memory_policy, active_workspace_key)
                       VALUES (1, '', '{}', 'ask', 'studypal')"""
                )
                conn.commit()
                row = conn.execute('SELECT * FROM user_profile WHERE id=1').fetchone()
            result = _row(row)
            result['preferences'] = json.loads(result.pop('preferences_json') or '{}')
            return result
        finally:
            conn.close()

    def update_profile(
        self,
        *,
        display_name: str | None = None,
        memory_policy: str | None = None,
        preferences: dict[str, Any] | None = None,
        active_workspace_key: str | None = None,
    ) -> dict[str, Any]:
        if memory_policy is not None and memory_policy not in {'ask', 'approved_types', 'all_local'}:
            raise ValueError(f'非法记忆策略：{memory_policy}')
        if preferences is not None:
            reject_secrets(preferences, 'preferences')
        current = self.get_profile()
        next_display = current['display_name'] if display_name is None else str(display_name)
        next_policy = current['memory_policy'] if memory_policy is None else memory_policy
        next_preferences = current['preferences'] if preferences is None else preferences
        next_workspace = current['active_workspace_key'] if active_workspace_key is None else str(active_workspace_key)
        conn = self._connection_factory()
        try:
            conn.execute(
                """UPDATE user_profile
                   SET display_name=?, preferences_json=?, memory_policy=?,
                       active_workspace_key=?, updated_at=datetime('now','localtime')
                   WHERE id=1""",
                (next_display, _json(next_preferences), next_policy, next_workspace),
            )
            conn.commit()
        finally:
            conn.close()
        return self.get_profile()

    def create_goal(
        self,
        *,
        workspace_key: str,
        title: str,
        description: str = '',
        priority: int = 3,
        due_at: str = '',
        next_action: str = '',
        source_type: str = '',
        source_id: str = '',
    ) -> dict[str, Any]:
        if not workspace_key.strip() or not title.strip():
            raise ValueError('workspace_key 和 title 不能为空')
        if type(priority) is not int or not 1 <= priority <= 5:
            raise ValueError('priority 必须是 1 到 5')
        conn = self._connection_factory()
        try:
            cur = conn.execute(
                """INSERT INTO agent_goals
                   (workspace_key, title, description, priority, due_at, next_action, source_type, source_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (workspace_key, title.strip(), description, priority, due_at, next_action, source_type, source_id),
            )
            conn.commit()
            return _row(conn.execute('SELECT * FROM agent_goals WHERE id=?', (cur.lastrowid,)).fetchone())
        finally:
            conn.close()

    def list_goals(self, *, workspace_key: str, status: str | None = None) -> list[dict[str, Any]]:
        conn = self._connection_factory()
        try:
            sql = 'SELECT * FROM agent_goals WHERE workspace_key=?'
            args: list[Any] = [workspace_key]
            if status is not None:
                sql += ' AND status=?'
                args.append(status)
            sql += ' ORDER BY priority, COALESCE(due_at, \"\"), id DESC'
            return [_row(row) for row in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    def get_run(self, run_id: int, *, workspace_key: str | None = None) -> dict[str, Any]:
        conn = self._connection_factory()
        try:
            sql = 'SELECT * FROM agent_runs WHERE id=?'
            args: list[Any] = [run_id]
            if workspace_key:
                sql += ' AND workspace_key=?'
                args.append(workspace_key)
            return _row(conn.execute(sql, args).fetchone())
        finally:
            conn.close()

    def update_run(self, run_id: int, *, status: str, summary: str = '', error: str = '') -> dict[str, Any]:
        allowed = {'queued', 'running', 'waiting_approval', 'succeeded', 'failed', 'cancelled'}
        if status not in allowed:
            raise ValueError(f'非法运行状态：{status}')
        conn = self._connection_factory()
        try:
            cur = conn.execute(
                'UPDATE agent_runs SET status=?, summary=?, error=? WHERE id=?',
                (status, summary, error, run_id),
            )
            if cur.rowcount != 1:
                raise ValueError(f'运行记录不存在：{run_id}')
            conn.commit()
            return _row(conn.execute('SELECT * FROM agent_runs WHERE id=?', (run_id,)).fetchone())
        finally:
            conn.close()

    def list_runs(
        self,
        *,
        workspace_key: str,
        statuses: set[str] | None = None,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        """Return bounded run summaries for the home surface.

        Raw model context is deliberately left in SQLite for local diagnostics,
        but callers receive only the lifecycle fields needed to resume work.
        """
        if not workspace_key.strip():
            raise ValueError('workspace_key 不能为空')
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('limit 必须在 1 到 200 之间')
        allowed = {'queued', 'running', 'waiting_approval', 'succeeded', 'failed', 'cancelled'}
        if statuses is not None and not statuses.issubset(allowed):
            raise ValueError('运行状态无效')
        conn = self._connection_factory()
        try:
            sql = 'SELECT * FROM agent_runs WHERE workspace_key=?'
            args: list[Any] = [workspace_key]
            if statuses:
                marks = ','.join('?' for _ in statuses)
                sql += f' AND status IN ({marks})'
                args.extend(sorted(statuses))
            sql += ' ORDER BY id DESC LIMIT ?'
            args.append(limit)
            rows = []
            for row in conn.execute(sql, args).fetchall():
                item = _row(row)
                if item.get('trigger_type') == 'workflow_waiting':
                    try:
                        workflow_input = json.loads(item.get('input_json') or '{}')
                    except (TypeError, json.JSONDecodeError):
                        workflow_input = {}
                    # The home page needs a safe deep link, while the raw
                    # runtime input remains private and bounded.
                    item['workflow_key'] = str(workflow_input.get('workflow_key') or '')[:64]
                    item['workflow_run_id'] = workflow_input.get('workflow_run_id')
                item.pop('input_json', None)
                item.pop('context_json', None)
                rows.append(item)
            return rows
        finally:
            conn.close()

    def get_goal(self, goal_id: int, *, workspace_key: str | None = None) -> dict[str, Any]:
        conn = self._connection_factory()
        try:
            sql = 'SELECT * FROM agent_goals WHERE id=?'
            args: list[Any] = [goal_id]
            if workspace_key:
                sql += ' AND workspace_key=?'
                args.append(workspace_key)
            return _row(conn.execute(sql, args).fetchone())
        finally:
            conn.close()

    def update_goal(self, goal_id: int, *, workspace_key: str, values: dict[str, Any]) -> dict[str, Any]:
        allowed = {'title', 'description', 'status', 'priority', 'due_at', 'next_action'}
        patch = {key: values[key] for key in values if key in allowed}
        if not patch:
            raise ValueError('没有可修改的目标字段')
        if 'title' in patch and not str(patch['title']).strip():
            raise ValueError('目标标题不能为空')
        if 'status' in patch and patch['status'] not in {'active', 'paused', 'completed', 'archived'}:
            raise ValueError('目标状态无效')
        if 'priority' in patch and (type(patch['priority']) is not int or not 1 <= patch['priority'] <= 5):
            raise ValueError('priority 必须是 1 到 5')
        conn = self._connection_factory()
        try:
            if not conn.execute('SELECT id FROM agent_goals WHERE id=? AND workspace_key=?', (goal_id, workspace_key)).fetchone():
                raise ValueError('目标不存在或不属于当前工作区')
            sets = ','.join(f'{key}=?' for key in patch)
            conn.execute(
                f"UPDATE agent_goals SET {sets}, updated_at=datetime('now','localtime') WHERE id=? AND workspace_key=?",
                [*patch.values(), goal_id, workspace_key],
            )
            conn.commit()
            return _row(conn.execute('SELECT * FROM agent_goals WHERE id=?', (goal_id,)).fetchone())
        finally:
            conn.close()
