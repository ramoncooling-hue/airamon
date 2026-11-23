import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.memory import platform_manual


class PlatformManualTests(unittest.TestCase):
    def test_search_returns_authoritative_platform_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'usage.md').write_text(
                '# 课程投放\n\n先选课程，再点击投放区上传 PDF 或 Markdown。\n\n'
                '# 图谱确认\n\n图谱候选需要点击确认入图谱后才会成为正式知识点。\n',
                encoding='utf-8',
            )
            with patch.object(platform_manual, 'DOCS_DIR', root):
                hits = platform_manual.search('怎么上传课程材料', limit=3)
            self.assertTrue(hits)
            self.assertEqual(hits[0]['kind'], 'manual')
            self.assertEqual(hits[0]['course_name'], 'MinDock 平台')
            self.assertIn('投放区', hits[0]['gist'])
            self.assertIn('usage.md', hits[0]['source'])

    def test_platform_question_detection_covers_how_to_use_requests(self):
        self.assertTrue(platform_manual.should_search('这个平台怎么使用？'))
        self.assertTrue(platform_manual.should_search('新增课程之后文件存在哪里'))
        self.assertTrue(platform_manual.should_search('手册该怎么干？'))
        self.assertTrue(platform_manual.should_search('图谱候选怎么确认？'))
        self.assertTrue(platform_manual.should_search('怎么接入 MCP？'))
        self.assertTrue(platform_manual.should_search('如何配置 mcp？'))
        self.assertFalse(platform_manual.should_search('边际成本是什么？'))

    def test_mcp_manual_contains_current_settings_flow(self):
        hits = platform_manual.search('MCP 接入 配置', limit=5)
        self.assertTrue(any('连接并发现工具' in hit['detail'] for hit in hits))

    def test_mapping_rule_help_is_detected_and_explains_the_approval_boundary(self):
        self.assertTrue(platform_manual.should_search('整理规则怎么用？'))
        self.assertTrue(platform_manual.should_search('映射草稿如何确认？'))
        hits = platform_manual.search('整理规则怎么用', limit=3)
        rule = next(hit for hit in hits if '整理规则' in hit['name'])
        self.assertIn('人工核对并勾选', rule['detail'])
        self.assertIn('不表示新增知识点已经进入图谱', rule['detail'])
        self.assertIn('/api/manual/open?', rule['manual_url'])

    def test_manual_hits_include_deep_link_to_document_and_line(self):
        hits = platform_manual.search('课程材料上传与存储', limit=10)
        self.assertTrue(hits)
        self.assertTrue(all(hit['manual_url'].startswith('/api/manual/open?path=') for hit in hits))
        self.assertRegex(hits[0]['manual_url'], r'&line=\d+#line-\d+$')


if __name__ == '__main__':
    unittest.main()
