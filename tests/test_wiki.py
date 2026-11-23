import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.memory import wiki


class WikiArtifactTests(unittest.TestCase):
    def test_render_has_contract_sections_and_source_content(self):
        note = wiki.render_note(
            course='测试课程', source_path='/tmp/lesson.md', source_sha256='abc',
            content='# 成本核算\n边际成本来自原文。', headings=['成本核算'],
            evidence=[{'locator_type': 'line', 'locator_start': 2, 'text': '边际成本来自原文。'}],
        )
        for section in ('## 摘要', '## 关键概念', '## 详细内容', '## 溯源', '## 相关链接'):
            self.assertIn(section, note)
        self.assertIn('边际成本来自原文。', note)
        self.assertNotIn('(待补充)', note)

    def test_write_artifacts_creates_note_and_dictionary(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = wiki.write_artifacts(
                course_dir=Path(tmp) / '会计学', course='会计学', source_path='lesson.md',
                source_sha256='abc', content='内容', headings=['成本'], evidence=[],
            )
            self.assertTrue(Path(result['note']).exists())
            self.assertTrue(Path(result['dictionary']).exists())
            self.assertIn('cyberpal.dictionary.v1', Path(result['dictionary']).read_text())

    def test_parse_assisted_response_requires_valid_evidence_refs(self):
        raw = '''```json
        {"summary":"边际成本描述新增产量的成本。","concepts":[
          {"name":"边际成本","kp_type":"concept","chapter":"成本","gist":"新增产量的成本","detail":"用于分析增量决策。","trap":"不要等同于平均成本","evidence_refs":[1]}
        ]}
        ```'''
        parsed = wiki.parse_assisted_response(raw, evidence_count=2)
        self.assertEqual(parsed['summary'], '边际成本描述新增产量的成本。')
        self.assertEqual(parsed['concepts'][0]['name'], '边际成本')
        with self.assertRaises(ValueError):
            wiki.parse_assisted_response(raw.replace('[1]', '[3]'), evidence_count=2)

    def test_assisted_draft_writes_a_reviewable_note_without_formal_graph_write(self):
        response = '{"summary":"原文摘要。","concepts":[{"name":"边际成本","kp_type":"concept","chapter":"成本","gist":"新增产量的成本","detail":"","trap":"","evidence_refs":[1]}]}'
        with tempfile.TemporaryDirectory() as tmp, patch.object(wiki.llm, 'chat', return_value=response):
            result = wiki.assist_draft(
                course_dir=Path(tmp) / '会计学', course='会计学', source_path='lesson.md',
                source_sha256='abc', content='# 成本\n边际成本来自原文。', headings=['成本'],
                evidence=[{'locator_type':'line','locator_start':2,'locator_end':2,'text':'边际成本来自原文。'}],
            )
            note = Path(result['note']).read_text(encoding='utf-8')
            dictionary = Path(result['dictionary']).read_text(encoding='utf-8')
            self.assertIn('status: assisted_draft', note)
            self.assertIn('原文摘要。', note)
            self.assertIn('cyberpal.dictionary.v1', dictionary)
            self.assertEqual(result['candidate_count'], 1)


if __name__ == '__main__':
    unittest.main()
