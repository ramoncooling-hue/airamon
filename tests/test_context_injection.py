"""T1.17: ContextCompiler 产物必须真正渲染进模型上下文；policy=off 不注入。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agent import chat
from app.agent_runtime.context import ContextCompiler
from app.agent_runtime.service import PersonalAgentService
from app.memory import chat_repo, db, repo


def minimal_context(**overrides):
    context = {
        'workspace': {'key': 'studypal'},
        'course': None,
        'question': '',
        'profile': {'display_name': '', 'memory_policy': 'ask', 'preferences': {}},
        'goals': [],
        'agenda': {'has_query': False, 'range_label': '', 'items': [], 'next_items': []},
        'memory': [],
        'memory_layers': [],
        'graph': [],
        'pending_actions': [],
        'conversation': {'id': None, 'messages': []},
        'limits': {'memory_layers': 10},
    }
    context.update(overrides)
    return context


class ContextRenderingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.app_dir = root / 'app'
        self.db_path = self.app_dir / 'context-injection-test.db'
        self.patches = [
            patch.object(db, 'APP_DIR', self.app_dir),
            patch.object(db, 'DB_PATH', self.db_path),
        ]
        for item in self.patches:
            item.start()
        db.apply_schema()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def test_renders_profile_layered_memory_and_conversation(self):
        context = minimal_context(
            profile={
                'display_name': '小思',
                'memory_policy': 'ask',
                'preferences': {'回答风格': '简洁', '语言': '中文'},
            },
            memory_layers=[{
                'source_id': 'memory_block:1',
                'layer': 'identity',
                'title': '项目约定',
                'content': '项目代号蓝鲸',
                'status': 'active',
                'confirmed': True,
            }],
            conversation={'id': 7, 'messages': [
                {'role': 'user', 'content': '继续讲上下文注入'},
                {'role': 'assistant', 'content': '好的，继续。'},
            ]},
        )
        block = chat.build_personal_context_block(context)
        self.assertIn('[personal agent context]', block)
        self.assertIn('用户称呼：小思', block)
        self.assertIn('用户偏好：', block)
        self.assertIn('- 回答风格：简洁', block)
        self.assertIn('分层记忆：', block)
        self.assertIn('项目代号蓝鲸', block)
        self.assertIn('已确认', block)
        self.assertIn('当前会话最近消息：', block)
        self.assertIn('- 用户：继续讲上下文注入', block)
        self.assertIn('- 助手：好的，继续。', block)

    def test_memory_policy_off_suppresses_whole_block(self):
        context = minimal_context(
            profile={'display_name': '小思', 'memory_policy': 'off', 'preferences': {}},
            memory=[{'source_id': 'memory:1', 'name': 'kp', 'gist': 'kp:17'}],
            memory_layers=[{
                'layer': 'identity', 'title': '旧记忆', 'content': '不应注入',
                'status': 'active', 'confirmed': True,
            }],
        )
        self.assertEqual(chat.build_personal_context_block(context), '')

    def test_ask_policy_only_renders_confirmed_memory(self):
        unconfirmed = {
            'layer': 'working', 'title': '未确认草稿', 'content': '草稿内容',
            'status': 'draft', 'confirmed': False,
        }
        confirmed = {
            'layer': 'working', 'title': '已确认结论', 'content': '确定内容',
            'status': 'active', 'confirmed': True,
        }
        ask_block = chat.build_personal_context_block(minimal_context(
            memory_layers=[unconfirmed, confirmed]))
        self.assertIn('已确认结论', ask_block)
        self.assertNotIn('未确认草稿', ask_block)

        all_block = chat.build_personal_context_block(minimal_context(
            profile={'display_name': '', 'memory_policy': 'all_local', 'preferences': {}},
            memory_layers=[unconfirmed, confirmed]))
        self.assertIn('未确认草稿', all_block)
        self.assertIn('已确认结论', all_block)

    def test_policy_off_keeps_memory_out_of_real_messages(self):
        context = minimal_context(
            profile={'display_name': '', 'memory_policy': 'off', 'preferences': {}},
            memory=[{'source_id': 'memory:1', 'name': 'kp', 'gist': 'kp:17'}],
        )
        messages = chat.build_messages(
            '问题', [], 'laoba', '全部课程', personal_context=context)
        rendered = '\n'.join(
            item['content'] for item in messages if item.get('role') == 'system')
        self.assertNotIn('[personal agent context]', rendered)
        self.assertNotIn('kp:17', rendered)

    def test_second_round_prompt_carries_confirmed_memory_and_previous_turns(self):
        service = PersonalAgentService()
        service.update_profile(display_name='小思', preferences={'回答风格': '简洁'})
        compiler = ContextCompiler()
        captured = {}

        def fake_model(tier, messages, tools, executor, **kwargs):
            captured['messages'] = messages
            return '第一轮回答', [], ''

        first = compiler.build_context(workspace_key='studypal', question='你好')
        with patch.object(chat.llm, 'chat_with_tools', side_effect=fake_model):
            result = chat.answer('你好', [], web_mode='off', personal_context=first)
        self.assertTrue(result['ok'], result)
        self.assertNotIn('项目代号蓝鲸', str(captured['messages']))

        repo.create_memory_block('studypal', {
            'layer': 'identity',
            'title': '项目约定',
            'content': '项目代号蓝鲸，后续回答要引用它',
        })
        conversation = chat_repo.create_conversation(title='测试会话')
        chat_repo.add_message(conversation['id'], 'user', '你好')
        chat_repo.add_message(
            conversation['id'], 'assistant', '第一轮回答',
            metadata={'raw_text': '第一轮回答'})

        second = compiler.build_context(
            workspace_key='studypal', conversation_id=conversation['id'],
            question='继续')
        history = [
            {'role': 'user', 'content': '你好'},
            {'role': 'assistant', 'content': '第一轮回答'},
        ]
        with patch.object(chat.llm, 'chat_with_tools', side_effect=fake_model):
            result = chat.answer(
                '继续', [], web_mode='off', personal_context=second, history=history)
        self.assertTrue(result['ok'], result)
        system_blocks = [
            item['content'] for item in captured['messages'] if item.get('role') == 'system'
        ]
        rendered = '\n'.join(system_blocks)
        self.assertIn('项目代号蓝鲸', rendered)
        self.assertIn('用户称呼：小思', rendered)
        # 显式 history 已覆盖会话内容，不重复渲染会话窗口。
        self.assertNotIn('当前会话最近消息', rendered)
        self.assertTrue(any(item.get('content') == '第一轮回答'
                            for item in captured['messages']))


if __name__ == '__main__':
    unittest.main()
