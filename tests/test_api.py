import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.api import routes
from app.agent import web_search
from app.memory import db
from app.memory import sources


class WebSaveApiTests(unittest.TestCase):
    def test_workspace_preference_update_preserves_profile_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'):
                db.apply_schema()
                routes.agent_profile_write(routes.AgentProfileIn(display_name='Reader',
                    memory_policy='approved_types', preferences={'language':'zh'}))
                result = routes.agent_profile_write(routes.AgentProfileIn(active_workspace_key='studypal'))
                self.assertEqual(result['display_name'], 'Reader')
                self.assertEqual(result['memory_policy'], 'approved_types')
                self.assertEqual(result['preferences'], {'language':'zh'})
                self.assertEqual(result['active_workspace_key'], 'studypal')

    def test_agent_api_returns_persistent_manual_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'), \
                 patch.object(routes.agent_chat, 'answer', return_value={
                     'ok': True, 'text': '按手册操作 [手册1]', 'error': '', 'hits': [],
                     'agenda': None, 'web_results': [], 'web_error': '', 'web_query': '',
                     'manual_results': [{'citation': '手册1', 'manual_url': '/api/manual/open?path=x&line=1'}],
                     'trace': [],
                 }), patch.object(routes.llm, 'available', return_value=True):
                db.apply_schema()
                result = routes.agent_ask(routes.AskIn(question='手册该怎么干？', web_mode='off'))
                self.assertEqual(result['manual_results'][0]['citation'], '手册1')

    def test_platform_manual_open_highlights_requested_line_and_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            guide = root / 'guide.md'
            guide.write_text('# 操作\n第一步\n第二步', encoding='utf-8')
            with patch.object(routes.platform_manual, 'DOCS_DIR', root):
                response = routes.platform_manual_open('guide.md', 3)
                self.assertEqual(response.status_code, 200)
                self.assertIn('id="line-3" class="line focus"', response.body.decode())
                with self.assertRaises(Exception):
                    routes.platform_manual_open('../outside.md', 1)

    def test_web_material_is_saved_and_indexed_for_course(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'APP_DIR', root / 'app'), \
                 patch.object(db, 'DB_PATH', root / 'app' / 'test.db'), \
                 patch.object(routes, 'STUDYPAL_DIR', root / 'StudyPal'), \
                 patch.object(web_search, '_public_url', side_effect=lambda url: url):
                db.apply_schema()
                conn = db.connect()
                conn.execute("INSERT INTO courses(name) VALUES('会计学')")
                conn.commit()
                conn.close()

                result = routes.web_save(routes.WebSaveIn(
                    course='会计学',
                    title='供应链韧性研究',
                    url='https://example.com/article',
                    snippet='摘要内容',
                    body='正文内容：供应链需要具备可见性。',
                    retrieved_at='2026-09-28T10:00:00+00:00',
                ))

                self.assertTrue(result['ok'])
                self.assertEqual(result['course'], '会计学')
                target = root / 'StudyPal' / '会计学' / 'inbox' / result['filename']
                self.assertTrue(target.exists())
                self.assertIn('https://example.com/article', target.read_text(encoding='utf-8'))
                self.assertEqual(result['index']['status'], 'indexed')

    def test_wiki_draft_endpoint_requires_indexed_source_and_returns_review_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'):
                db.apply_schema()
                conn = db.connect()
                conn.execute("INSERT INTO courses(name) VALUES('会计学')")
                conn.commit()
                conn.close()
                inbox = root / '会计学' / 'inbox'
                inbox.mkdir(parents=True)
                doc = inbox / 'lesson.md'
                doc.write_text('# 成本\n边际成本来自原文。', encoding='utf-8')
                sources.scan_course('会计学', [inbox])
                response = '{"summary":"摘要。","concepts":[{"name":"边际成本","kp_type":"concept","chapter":"成本","gist":"新增成本","evidence_refs":[2]}]}'
                with patch.object(sources.wiki.llm, 'chat', return_value=response):
                    result = routes.source_wiki_draft(routes.WikiDraftIn(
                        course='会计学', path=str(doc.resolve()), tier='fast'
                    ))
                self.assertEqual(result['status'], 'assisted_draft')
                self.assertTrue(Path(result['note']).exists())


if __name__ == '__main__':
    unittest.main()
