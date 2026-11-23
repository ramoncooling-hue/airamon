import json
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agent import chat, llm
from app.agent import web_search
from app.memory import chat_repo, db
from tests.test_web_search import _Opener


class AgentRuntimeTests(unittest.TestCase):
    def test_system_for_reads_enabled_file_backed_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agents = root / 'agents' / 'custom'
            agents.mkdir(parents=True)
            (agents / 'system.md').write_text('CUSTOM SYSTEM', encoding='utf-8')
            (agents / 'manifest.json').write_text(
                '{"key":"custom","name":"Custom","version":"1.0.0"}', encoding='utf-8'
            )
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'), \
                 patch.object(chat, 'AGENTS_DIR', root / 'agents'):
                db.apply_schema()
                self.assertEqual(chat.system_for('custom'), 'CUSTOM SYSTEM')

    def test_system_for_falls_back_when_file_backed_agent_is_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agents = root / 'agents' / 'custom'
            agents.mkdir(parents=True)
            (agents / 'system.md').write_text('DISABLED SYSTEM', encoding='utf-8')
            (agents / 'manifest.json').write_text(
                '{"key":"custom","name":"Custom","version":"1.0.0"}', encoding='utf-8'
            )
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'), \
                 patch.object(chat, 'AGENTS_DIR', root / 'agents'), patch.object(chat.registry, 'AGENTS_DIR', root / 'agents'):
                db.apply_schema()
                chat.registry.sync()
                chat.registry.set_enabled('agent', 'custom', False)
                self.assertNotEqual(chat.system_for('custom'), 'DISABLED SYSTEM')

    def test_incomplete_research_continues_instead_of_asking_permission(self):
        def model(tier, messages, tools, executor, **kwargs):
            executor('web_search', {'query':'OpenAI Astra'})
            feedback = kwargs['review_completion']('要不要我去核对官方来源？')
            self.assertIn('web_open', feedback)
            executor('web_open', {'url':'https://example.com/astra'})
            self.assertEqual(kwargs['review_completion']('已读取来源并整理。'), '')
            return '已读取来源并整理。', [], ''
        with patch.object(llm, 'chat_with_tools', side_effect=model), patch.object(chat.web_search, 'search', return_value=[{'url':'https://example.com/astra'}]), patch.object(chat.web_search, 'open_page', return_value={'url':'https://example.com/astra','body':'原文'}):
            result = chat.answer('Astra', [])
        self.assertTrue(result['ok'])
        self.assertEqual([e['tool'] for e in result['trace']], ['web_search','web_open'])

    def test_streaming_assembles_tool_arguments_and_emits_text(self):
        chunks = [
            {'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'a', 'function': {'name': 'web_search', 'arguments': '{"query":'}}]}}]},
            {'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': '"OpenAI Astra"}'}}]}}]},
            {'choices': [{'delta': {'content': '已查到'}}]},
        ]
        stream = io.BytesIO(('\n'.join('data: '+json.dumps(x) for x in chunks)+'\ndata: [DONE]\n').encode())
        events = []
        msg = llm._read_tool_stream(stream, events.append)['choices'][0]['message']
        self.assertEqual(json.loads(msg['tool_calls'][0]['function']['arguments']), {'query': 'OpenAI Astra'})
        self.assertEqual(events, [{'type': 'delta', 'text': '已查到'}])

    def test_search_retries_noise_and_reports_actual_query(self):
        noise = [{'title': 'OpenAI 官网入口', 'url': 'https://example.com/home', 'snippet': ''}]
        good = [{'title': 'OpenAI Astra', 'url': 'https://example.com/astra', 'snippet': '', 'query': 'OpenAI Astra'}]
        with patch.object(web_search.store, 'read_raw', return_value={}), patch.object(web_search, '_bing_rss', side_effect=[noise, good]) as search:
            rows = web_search.search('OpenAI Astra 发布')
        self.assertEqual(rows[0]['query'], 'OpenAI Astra')
        self.assertEqual(len(rows), 1)
        self.assertEqual(search.call_count, 2)

    def test_web_off_removes_web_tools_and_local_scope_is_preserved(self):
        def model(tier, messages, tools, executor, **kwargs):
            self.assertNotIn('web_search', [t['function']['name'] for t in tools])
            self.assertIn('error', executor('web_search', {'query':'x'}))
            executor('search_memory', {'query': '资产负债表'})
            executor('query_agenda', {'query': '27号作业'})
            return '答案', [], ''
        with patch.object(llm, 'chat_with_tools', side_effect=model), patch.object(chat.repo, 'search', return_value=[]) as local, patch.object(chat.sources, 'search', return_value=[]), patch.object(chat.repo, 'query_agenda', return_value={'items': [], 'has_query':True}) as agenda:
            result = chat.answer('27号作业', [], scope_label='会计学', web_mode='off')
        # search 现在还会传 workspace（最后一个位置参数），scope 仍是课程标签。
        self.assertIn('会计学', local.call_args.args)
        self.assertEqual(agenda.call_args.args[-1], '会计学')
        self.assertEqual(result['web_results'], [])

    def test_read_failure_preserves_successful_search_evidence(self):
        def model(tier, messages, tools, executor, **kwargs):
            executor('web_search', {'query': 'OpenAI Astra'})
            self.assertIn('error', executor('web_open', {'url': 'https://example.com/astra'}))
            return '原文不可读，只有摘要。', [], ''
        with patch.object(llm, 'chat_with_tools', side_effect=model), patch.object(chat.web_search, 'search', return_value=[{'url': 'https://example.com/astra'}]), patch.object(chat.web_search, 'open_page', side_effect=RuntimeError('HTTP 403')):
            result = chat.answer('Astra', [])
        self.assertTrue(result['ok'])
        self.assertEqual(len(result['web_results']), 1)
        self.assertIn('403', result['web_error'])
        self.assertEqual(result['trace'][-1]['status'], 'error')

    def test_new_topic_and_model_query_are_not_overwritten(self):
        history = [{'role': 'user', 'content': '宁德时代2025年财报'}]
        self.assertEqual(chat.contextual_search_query('检索OpenAI最近新闻', history), '检索OpenAI最近新闻')
        self.assertEqual(chat.contextual_search_query('还有Astra呢', history, 'OpenAI Astra'), 'OpenAI Astra')

    def test_normal_chat_has_optional_tools_and_plain_user_message(self):
        seen = {}
        def model(tier, messages, tools, executor, **kwargs):
            seen.update(messages=messages, tools=tools, **kwargs)
            return '你好，有什么想聊的？', [], ''
        with patch.object(llm, 'chat_with_tools', side_effect=model):
            out = chat.answer('你好', [], web_mode='auto')
        self.assertTrue(out['ok'])
        self.assertFalse(seen['force_tool'])
        self.assertEqual(seen['messages'][-1]['content'], '你好')
        self.assertIn('web_search', [t['function']['name'] for t in seen['tools']])
        self.assertIn('search_memory', [t['function']['name'] for t in seen['tools']])

    def test_platform_question_preloads_manual_evidence_without_tool_call(self):
        seen = {}
        manual = [{'kind':'manual','name':'课程页怎么用','source':'docs/platform/guide.md:12',
                   'detail':'选择课程后上传材料。','citation':'手册1','manual_url':'/api/manual/open?path=x&line=12'}]
        def model(tier, messages, tools, executor, **kwargs):
            seen['messages'] = messages
            seen['force_tool'] = kwargs['force_tool']
            return '先选课程，再上传材料 [手册1]。', [], ''
        with patch.object(chat.platform_manual, 'search', return_value=manual), \
             patch.object(llm, 'chat_with_tools', side_effect=model):
            result = chat.answer('手册该怎么干？', [], web_mode='off')
        self.assertTrue(result['ok'])
        self.assertFalse(seen['force_tool'])
        self.assertIn('[手册1]', '\n'.join(m['content'] for m in seen['messages']))
        self.assertEqual(result['manual_results'][0]['manual_url'], manual[0]['manual_url'])

    def test_open_official_url_does_not_require_a_search_first(self):
        def model(tier, messages, tools, executor, **kwargs):
            result = executor('web_open', {'url': 'https://example.com/news'})
            self.assertNotIn('error', result)
            return '来源', result['results'], ''
        with patch.object(llm, 'chat', side_effect=AssertionError('unexpected plain chat')), patch.object(llm, 'chat_with_tools', side_effect=model), patch.object(chat.web_search, 'open_page', return_value={'url': 'https://example.com/news', 'body': '新闻'}):
            self.assertTrue(chat.answer('读这个网站', [])['ok'])

    def test_budget_synthesizes_with_existing_tool_evidence(self):
        call = {'choices': [{'message': {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'a', 'type': 'function', 'function': {'name': 'web_search', 'arguments': '{"query":"Astra"}'}}
        ]}}]}
        tool = {'type': 'function', 'function': {'name': 'web_search'}}
        with patch.object(llm, '_payload', return_value=('http://test', {}, {})), patch.object(llm, '_OPENER', _Opener([call])), patch.object(llm, 'chat', return_value='结论') as synth:
            text, results, error = llm.chat_with_tools('fast', [], [tool], lambda n, a: {'results': [{'url': 'https://example.com'}]}, max_rounds=1)
        self.assertEqual(text, '结论')
        self.assertTrue(any(m['role'] == 'tool' for m in synth.call_args.args[1]))
        self.assertEqual(len(results), 1)

    def test_evidence_survives_database_reload(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(db, 'APP_DIR', Path(tmp)), patch.object(db, 'DB_PATH', Path(tmp) / 'test.db'):
            db.apply_schema()
            conv = chat_repo.create_conversation()
            evidence = {'raw_text': '回答', 'web_results': [{'url': 'https://example.com'}], 'trace': [{'tool': 'web_open'}]}
            chat_repo.add_message(conv['id'], 'assistant', '<p>回答</p>', metadata=evidence)
            db.apply_schema()
            self.assertEqual(chat_repo.get_conversation(conv['id'])['messages'][0]['metadata'], evidence)

    def test_old_manual_answer_is_hydrated_from_saved_query_trace(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(db, 'APP_DIR', Path(tmp)), patch.object(db, 'DB_PATH', Path(tmp) / 'test.db'):
            db.apply_schema()
            conv = chat_repo.create_conversation()
            chat_repo.add_message(
                conv['id'], 'assistant', '<p>旧回答 [手册1]</p>',
                metadata={'raw_text': '旧回答 [手册1]', 'manual_results': [],
                          'trace': [{'tool': 'query_manual', 'query': '怎么上传课程材料'}]},
            )
            loaded = chat_repo.get_conversation(conv['id'])['messages'][0]['metadata']
            self.assertTrue(loaded['manual_results'])
            self.assertTrue(loaded['manual_results'][0]['manual_url'].startswith('/api/manual/open?'))


if __name__ == '__main__':
    unittest.main()
