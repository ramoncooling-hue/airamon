"""Permission and workspace rules for Personal Agent actions."""

from __future__ import annotations

from .actions import ALLOWED_ACTIONS

READ_ONLY = {
    'search_memory', 'query_agenda', 'query_manual', 'web_search', 'web_open',
}
EXTERNAL_SIDE_EFFECTS = {
    'send_email', 'send_message', 'publish_web', 'call_external_api',
}


class ActionPolicy:
    def classify(self, action_kind: str) -> str:
        if action_kind in READ_ONLY:
            return 'read_only'
        if action_kind in ALLOWED_ACTIONS:
            return 'local_write'
        if action_kind in EXTERNAL_SIDE_EFFECTS:
            return 'external_side_effect'
        raise ValueError(f'未知动作类型：{action_kind}')

    def requires_confirmation(self, action_kind: str) -> bool:
        return self.classify(action_kind) != 'read_only'

    @staticmethod
    def assert_workspace(source_workspace: str, target_workspace: str) -> None:
        if not source_workspace or not target_workspace:
            raise ValueError('工作区不能为空')
        if source_workspace != target_workspace:
            raise ValueError('动作不能跨工作区执行')
