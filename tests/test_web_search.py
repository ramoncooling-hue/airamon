import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.agent import llm
from app.agent import chat as agent_chat
from app.agent import web_search
from app.agent.web_search import open_page, should_search


class _Response:
    def __init__(self, payload):
        self.payload = payload
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        return json.dumps(self.payload).encode()


class _Opener:
    def __init__(self, responses):
        self.responses = iter(responses)
    def open(self, request, timeout=0):
        return _Response(next(self.responses))


class _PageHeaders:
    def __init__(self, content_type):
        self.content_type = content_type
    def get_content_type(self):
        return self.content_type


class _PageResponse:
    def __init__(self, html, url='https://example.com/article'):
        self.html = html.encode()
        self.headers = _PageHeaders('text/html')
        self.url = url
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self, limit=-1):
        return self.html[:limit] if limit >= 0 else self.html
    def geturl(self):
        return self.url


class WebSearchPolicyTests(unittest.TestCase):
    def test_follow_up_search_keeps_previous_topic(self):
        history = [
            {'role': 'user', 'content': '帮我搜索一下宁德时代的25年财报'},
            {'role': 'assistant', 'content': '我会帮你查找。'},
        ]
        query = agent_chat.contextual_search_query('现在已经是2026年10月了', history, '现在')
        self.assertIn('宁德时代', query)
        self.assertIn('财报', query)
        self.assertIn('2026年10月', query)

    def test_vague_model_query_is_replaced_by_conversation_topic(self):
        history = [{'role': 'user', 'content': '我想了解中国制造业的供应链韧性'}]
        query = agent_chat.contextual_search_query('继续', history, '帮')
        self.assertIn('供应链韧性', query)
        self.assertNotEqual(query, '帮')

    def test_first_turn_vague_model_query_falls_back_to_user_question(self):
        query = agent_chat.contextual_search_query('帮我搜索一下宁德时代的25年财报', [], '帮')
        self.assertIn('宁德时代', query)
        self.assertNotEqual(query, '帮')

    def test_model_refinement_is_not_overridden_by_host(self):
        query = agent_chat.contextual_search_query(
            '宁德时代2025年财报', [], '宁德时代 300750 2025年 业绩快报 营业收入 同比'
        )
        self.assertEqual(query, '宁德时代 300750 2025年 业绩快报 营业收入 同比')

    def test_complete_query_is_not_prefixed_with_old_topic(self):
        history = [{'role': 'user', 'content': '先聊一下 MBA 课程安排'}]
        query = agent_chat.contextual_search_query('宁德时代2025年财报', history, '宁德时代2025年财报')
        self.assertEqual(query, '宁德时代2025年财报')

    def test_search_preserves_complete_query_including_correction(self):
        variants = web_search._query_variants('帮我搜索一下宁德时代的25年财报；补充：现在已经是2026年10月了')
        self.assertEqual(variants[0], '帮我搜索一下宁德时代的25年财报；补充：现在已经是2026年10月了')

    def test_auto_web_intent_forces_host_search_tool(self):
        seen = {}

        def fake_tools(tier, messages, tools, executor, **kwargs):
            seen.update(kwargs)
            return '已检索', [{'title': '年报', 'url': 'https://example.com/report'}], ''

        with patch.object(llm, 'available', return_value=True), \
             patch.object(llm, 'chat_with_tools', side_effect=fake_tools):
            result = agent_chat.answer(
                '宁德时代2025年财报', [], history=[], web_mode='auto'
            )
        self.assertTrue(seen['force_tool'])
        # No executor call means no search happened: do not fabricate a trace.
        self.assertEqual(result['web_query'], '')

    def test_modes_are_shared_by_all_roles(self):
        self.assertTrue(should_search('随便问一个开放问题', 'laoba', 'on'))
        self.assertFalse(should_search('请搜索最新 MBA 趋势', 'paper', 'off'))
        self.assertTrue(should_search('请写一份市场调研初稿', 'paper', 'auto'))
        self.assertFalse(should_search('27号交什么作业', 'laoba', 'auto', has_agenda=True))
        self.assertTrue(should_search('宁德时代2025年财报', 'laoba', 'auto'))
        self.assertTrue(should_search('宁德时代2025年财报', 'laoba', 'auto', has_agenda=True))

    def test_openai_compatible_tool_loop_returns_tool_result_to_model(self):
        tool_reply = {
            'choices': [{'message': {'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': 'call-1', 'type': 'function',
                'function': {'name': 'web_search', 'arguments': '{"query":"MBA"}'},
            }]}}]
        }
        final_reply = {'choices': [{'message': {'role': 'assistant', 'content': '已根据网页资料整理。'}}]}
        tool = {'type': 'function', 'function': {'name': 'web_search', 'parameters': {'type': 'object'}}}
        with patch.object(llm, '_payload', return_value=('http://test', {'messages': []}, {})), \
             patch.object(llm, '_OPENER', _Opener([tool_reply, final_reply])):
            text, results, error = llm.chat_with_tools(
                'fast', [{'role': 'user', 'content': '查 MBA'}], [tool],
                lambda name, args: {'results': [{'title': args['query'], 'url': 'https://example.com'}]},
                force_tool=True,
            )
        self.assertEqual(text, '已根据网页资料整理。')
        self.assertEqual(results[0]['title'], 'MBA')
        self.assertEqual(error, '')

    def test_open_page_keeps_article_text_and_removes_page_chrome(self):
        html = (
            '<html><head><title>Article</title><style>hidden css</style></head>'
            '<body><nav>menu</nav><main><h1>正文标题</h1><p>正文内容</p>'
            '<script>secret()</script><footer>footer</footer></main></body></html>'
        )
        opener = type('StubOpener', (), {
            'open': lambda self, request, timeout=0: _PageResponse(html)
        })()
        with patch.object(web_search.urllib.request, 'build_opener', return_value=opener), \
             patch.object(web_search, '_public_url', side_effect=lambda url: url):
            result = open_page('https://example.com/article')
        self.assertIn('正文标题', result['body'])
        self.assertIn('正文内容', result['body'])
        self.assertNotIn('hidden css', result['body'])
        self.assertNotIn('secret()', result['body'])
        self.assertNotIn('menu', result['body'])
        self.assertNotIn('footer', result['body'])

    def test_open_page_truncates_large_body(self):
        html = '<main>' + ('正文 ' * 20000) + '</main>'
        opener = type('StubOpener', (), {
            'open': lambda self, request, timeout=0: _PageResponse(html)
        })()
        with patch.object(web_search.urllib.request, 'build_opener', return_value=opener), \
             patch.object(web_search, '_public_url', side_effect=lambda url: url):
            result = open_page('https://example.com/large')
        self.assertLessEqual(len(result['body']), 40000)

    def test_public_url_rejects_local_and_non_global_addresses(self):
        for url in ('http://localhost/article', 'http://127.0.0.1/article',
                    'http://192.168.1.10/article'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                web_search._public_url(url)

    def test_open_page_validates_redirect_target(self):
        class RedirectOpener:
            def open(self, request, timeout=0):
                raise AssertionError('redirect handler should reject before open')

        def build_opener(*handlers):
            redirect_handler = handlers[-1]
            redirect_handler.redirect_request(
                SimpleNamespace(full_url='https://example.com/redirect'),
                None, 302, 'Found', {}, 'http://127.0.0.1/private'
            )
            return RedirectOpener()

        with patch.object(web_search.urllib.request, 'build_opener', side_effect=build_opener):
            with self.assertRaises(ValueError):
                open_page('https://example.com/redirect')


if __name__ == '__main__':
    unittest.main()
