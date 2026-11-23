"""Bounded, source-aware context assembly for Personal Agent runs."""

from __future__ import annotations

import json
from typing import Any, Callable

from ..memory import chat_repo, db, repo
from .service import PersonalAgentService

MAX_MEMORY = 8
MAX_AGENDA = 14
MAX_GOALS = 8
MAX_MESSAGES = 8
MAX_ACTIONS = 8
MAX_GRAPH = 8
MAX_LAYERED_MEMORY = 10
MAX_TEXT = 1200


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    return ' '.join(str(value or '').split())[:limit]


def _source_id(row: dict[str, Any], default_kind: str) -> str:
    kind = str(row.get('source_type') or row.get('kind') or default_kind)
    raw_id = row.get('source_id') or row.get('id')
    return f'{kind}:{raw_id}' if raw_id not in (None, '') else kind


class ContextCompiler:
    def __init__(
        self,
        *,
        runtime: PersonalAgentService | None = None,
        memory_search: Callable[..., list[dict[str, Any]]] | None = None,
        agenda_query: Callable[..., dict[str, Any]] = repo.query_agenda,
        conversation_loader: Callable[[int], dict[str, Any] | None] = chat_repo.get_conversation,
        graph_loader: Callable[..., list[dict[str, Any]]] | None = None,
    ):
        self.runtime = runtime or PersonalAgentService()
        self.memory_search = memory_search or self._search_memory
        self.agenda_query = agenda_query
        self.conversation_loader = conversation_loader
        self.graph_loader = graph_loader

    def build_context(
        self,
        *,
        workspace_key: str,
        course_key: str | None = None,
        conversation_id: int | None = None,
        goal_id: int | None = None,
        question: str = '',
    ) -> dict[str, Any]:
        if not workspace_key.strip():
            raise ValueError('workspace_key 不能为空')

        profile = self.runtime.get_profile()
        goals = self.runtime.list_goals(workspace_key=workspace_key)
        if goal_id is not None:
            goals = [goal for goal in goals if goal['id'] == goal_id]
        goals_truncated = len(goals) > MAX_GOALS
        goals = [
            {
                'id': goal['id'],
                'title': _text(goal['title'], 180),
                'description': _text(goal['description'], 600),
                'status': goal['status'],
                'priority': goal['priority'],
                'due_at': goal['due_at'],
                'next_action': _text(goal['next_action'], 240),
                'source_id': _source_id(goal, 'goal'),
            }
            for goal in goals[:MAX_GOALS]
        ]

        agenda_raw = self.agenda_query(question, course_key, workspace_key) if question else {
            'has_query': False, 'items': [], 'next_items': []
        }
        agenda_items_raw = agenda_raw.get('items') or []
        agenda_next_raw = agenda_raw.get('next_items') or []
        agenda = {
            'has_query': bool(agenda_raw.get('has_query')),
            'range_label': _text(agenda_raw.get('range_label'), 120),
            'items': [self._agenda_item(item) for item in agenda_items_raw[:MAX_AGENDA]],
            'next_items': [self._agenda_item(item) for item in agenda_next_raw[:3]],
        }

        # Ask for one extra hit/row where cheap so a full page can report that
        # the list was clipped instead of silently dropping the overflow.
        hits = repo.dedupe_search_hits(
            (self.memory_search(question, MAX_MEMORY + 1, course_key, workspace_key) if question else []) or [])
        memory_hits = hits[:MAX_MEMORY]
        memory = [self._memory_item(item) for item in memory_hits]
        layered_raw = repo.search_memory_blocks(
            question, MAX_LAYERED_MEMORY + 1, workspace_key, include_stable=True
        )
        layered_memory = [self._layered_memory_item(item) for item in layered_raw[:MAX_LAYERED_MEMORY]]

        pending_actions = self._pending_actions(workspace_key)

        conversation = {'id': conversation_id, 'messages': []}
        conversation_truncated = False
        if conversation_id is not None:
            loaded = self.conversation_loader(conversation_id)
            if loaded is None:
                raise ValueError(f'会话不存在：{conversation_id}')
            if loaded.get('workspace_key', 'studypal') != workspace_key:
                raise ValueError('会话与当前工作区不一致')
            messages_raw = loaded.get('messages') or []
            conversation_truncated = len(messages_raw) > MAX_MESSAGES
            conversation['messages'] = [
                {
                    'role': message.get('role', ''),
                    'content': _text(message.get('content')),
                }
                for message in messages_raw[-MAX_MESSAGES:]
            ]

        graph = []
        graph_raw = []
        graph_loader = self.graph_loader or self._graph_context
        if graph_loader and question:
            graph_raw = list(graph_loader(course_key, workspace_key, question))
            graph = [dict(item) for item in graph_raw[:MAX_GRAPH]]

        truncated = {
            'goals': goals_truncated,
            'agenda': len(agenda_items_raw) > MAX_AGENDA,
            'memory': len(hits) > MAX_MEMORY,
            'snippets': any(item.get('truncated') for item in memory_hits),
            'memory_layers': len(layered_raw) > MAX_LAYERED_MEMORY,
            'graph': len(graph_raw) > MAX_GRAPH,
            'messages': conversation_truncated,
            'actions': False,
        }
        truncated['any'] = any(truncated.values())

        return {
            'workspace': {'key': workspace_key},
            'course': {'key': course_key} if course_key else None,
            'question': _text(question, 1000),
            'profile': {
                'display_name': _text(profile.get('display_name'), 120),
                'memory_policy': profile.get('memory_policy', 'ask'),
                'preferences': profile.get('preferences', {}),
            },
            'goals': goals,
            'agenda': agenda,
            'memory': memory,
            'memory_layers': layered_memory,
            'graph': graph,
            'pending_actions': pending_actions,
            'conversation': conversation,
            'truncated': truncated,
            'limits': {
                'memory': MAX_MEMORY,
                'agenda': MAX_AGENDA,
                'messages': MAX_MESSAGES,
                'actions': MAX_ACTIONS,
                'graph': MAX_GRAPH,
                'memory_layers': MAX_LAYERED_MEMORY,
                'text': MAX_TEXT,
            },
        }

    def _search_memory(
        self, question: str, limit: int = MAX_MEMORY,
        course_key: str | None = None, workspace_key: str | None = None,
    ) -> list[dict[str, Any]]:
        """Combine curated knowledge with indexed source fragments.

        The Personal Agent sees the same bounded local search surface as chat;
        it does not read course folders directly or bypass the memory layer.
        Shared retrieval helper dedupes by (kind/ref_kind, ref_id) so the same
        chunk cannot occupy several context slots.
        """
        return repo.memory_search_hits(question, limit, course_key, workspace_key)

    @staticmethod
    def _pending_actions(workspace_key: str) -> list[dict[str, Any]]:
        conn = db.connect()
        try:
            rows = conn.execute(
                """SELECT id, action_kind, target_type, target_id, proposal_json,
                          created_at
                     FROM agent_actions
                    WHERE workspace_key=? AND status='proposed'
                    ORDER BY id DESC LIMIT ?""",
                (workspace_key, MAX_ACTIONS),
            ).fetchall()
        finally:
            conn.close()
        result = []
        for row in rows:
            try:
                proposal = json.loads(row['proposal_json'] or '{}')
            except (TypeError, json.JSONDecodeError):
                proposal = {}
            result.append({
                'source_id': f"action:{row['id']}",
                'action_id': row['id'],
                'kind': row['action_kind'],
                'target_type': row['target_type'],
                'target_id': row['target_id'],
                'proposal': {key: _text(value, 240) for key, value in proposal.items()
                             if key not in {'content_base64', 'body'}},
                'created_at': row['created_at'],
            })
        return result

    @staticmethod
    def _graph_context(
        course_key: str | None, workspace_key: str, question: str,
    ) -> list[dict[str, Any]]:
        if not course_key:
            return []
        rows = repo.search(question, MAX_GRAPH, course_key, workspace_key)
        result: list[dict[str, Any]] = []
        seen: set[int] = set()
        for row in rows:
            try:
                kp_id = int(row.get('id'))
            except (TypeError, ValueError):
                continue
            if kp_id in seen:
                continue
            detail = repo.get_kp(kp_id)
            if not detail:
                continue
            seen.add(kp_id)
            result.append({
                'source_id': f"graph:{kp_id}",
                'id': kp_id,
                'name': _text(detail.get('name'), 180),
                'chapter': _text(detail.get('chapter'), 120),
                'mastery': _text(detail.get('mastery'), 40),
                'pre': [dict(item) for item in detail.get('pre', [])[:4]],
                'related': [dict(item) for item in detail.get('related', [])[:4]],
            })
        return result

    @staticmethod
    def _memory_item(item: dict[str, Any]) -> dict[str, Any]:
        return {
            'source_id': _source_id(item, 'memory'),
            'kind': item.get('kind', 'memory'),
            'name': _text(item.get('name'), 180),
            'gist': _text(item.get('gist') or item.get('detail')),
            'source': _text(item.get('source'), 240),
            'course_name': _text(item.get('course_name'), 120),
            'chapter': _text(item.get('chapter'), 120),
            'mastery': _text(item.get('mastery'), 40),
            'truncated': bool(item.get('truncated')),
        }

    @staticmethod
    def _layered_memory_item(item: dict[str, Any]) -> dict[str, Any]:
        return {
            'source_id': f"memory_block:{item.get('id')}",
            'id': item.get('id'),
            'layer': item.get('layer', 'working'),
            'title': _text(item.get('title'), 180),
            'content': _text(item.get('content'), 900),
            'status': item.get('status', 'active'),
            'confirmed': bool(item.get('confirmed', 0)),
            'confidence': item.get('confidence', 0.0),
            'source_type': item.get('source_type', 'manual'),
            'origin_source_id': item.get('source_id', ''),
        }

    @staticmethod
    def _agenda_item(item: dict[str, Any]) -> dict[str, Any]:
        return {
            'source_id': _source_id(item, 'event'),
            'title': _text(item.get('title'), 180),
            'date': _text(item.get('date'), 40),
            'course_name': _text(item.get('course_name'), 120),
            'status': _text(item.get('status'), 60),
            'note': _text(item.get('note'), 360),
        }


def build_context(**kwargs) -> dict[str, Any]:
    """Convenience boundary used by chat and scheduled runs."""
    return ContextCompiler().build_context(**kwargs)
