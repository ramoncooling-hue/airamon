import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.memory import db, repo, search_index, chat_repo
from app.memory import calendar_sync


class WorkspaceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [
            patch.object(db, 'APP_DIR', root / 'app'),
            patch.object(db, 'DB_PATH', root / 'app' / 'cyberpal.db'),
            patch.object(db, 'DATA_DIR', root / 'data'),
            patch.object(repo, 'DATA_DIR', root / 'data'),
        ]
        for item in self.patches:
            item.start()
        db.apply_schema()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def test_default_workspaces_and_legacy_records_are_studypal(self):
        self.assertEqual({row['key'] for row in repo.list_workspaces()}, {'studypal', 'lifepal', 'workpal'})
        course = repo.create_course('MBA 课程', workspace='studypal')
        event = repo.create_event({'title': 'StudyPal 作业', 'date': '2026-10-01', 'course_name': course['name']})
        self.assertEqual(course['workspace_key'], 'studypal')
        self.assertEqual(event['workspace_id'], course['workspace_id'])
        self.assertEqual([row['title'] for row in repo.list_events(workspace='lifepal')], [])

    def test_items_are_isolated_and_delete_removes_wiki_file(self):
        life = repo.create_workspace_item('lifepal', {'kind': 'note', 'title': '生活复盘', 'body': '连接 [[睡眠]]'})
        work = repo.create_workspace_item('workpal', {'kind': 'task', 'title': '工作任务', 'body': '跟进项目'})
        self.assertEqual([item['title'] for item in repo.list_workspace_items('lifepal')], ['生活复盘'])
        self.assertEqual([item['title'] for item in repo.list_workspace_items('workpal')], ['工作任务'])
        note_path = Path(life['path'])
        self.assertTrue(note_path.exists())
        text = note_path.read_text(encoding='utf-8')
        self.assertIn('schema: cyberpal.note.v2', text)
        self.assertIn('## 摘要', text)
        self.assertIn('## 关键概念', text)
        self.assertIn('## 详细内容', text)
        self.assertIn('## 相关链接', text)
        self.assertTrue(repo.delete_workspace_item(life['id']))
        self.assertFalse(note_path.exists())
        self.assertEqual(repo.list_workspace_items('lifepal'), [])
        self.assertTrue(repo.delete_workspace_item(work['id']))

    def test_calendar_exchange_keeps_workspace_boundary(self):
        life = repo.create_event({'workspace_id': repo.get_workspace('lifepal')['id'], 'title': '个人事项', 'date': '2026-10-02', 'category': 'personal'})
        study = repo.create_event({'workspace_id': repo.get_workspace('studypal')['id'], 'title': '课程事项', 'date': '2026-10-03', 'category': 'class'})
        exported = calendar_sync.export_ics(workspace='lifepal')
        self.assertIn('个人事项', exported)
        self.assertNotIn('课程事项', exported)
        calendar_sync.import_ics(
            'BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:life-new\nDTSTART;VALUE=DATE:20261004\nSUMMARY:新个人事项\nCATEGORIES:personal\nEND:VEVENT\nEND:VCALENDAR',
            workspace='lifepal',
        )
        self.assertEqual([row['title'] for row in repo.list_events(workspace='lifepal')], ['个人事项', '新个人事项'])
        self.assertEqual([row['title'] for row in repo.list_events(workspace='studypal')], ['课程事项'])

    def test_search_index_filters_workspace_before_limit(self):
        study = repo.create_course('学习课程', workspace='studypal')
        life = repo.create_course('生活课程', workspace='lifepal')
        conn = db.connect()
        try:
            for course, name in ((study, '共同主题'), (life, '目标主题')):
                conn.execute('INSERT INTO kp(course_id,name,gist,detail,trap,related,source) VALUES(?,?,?,?,?,?,?)',
                             (course['id'], name, '目标主题内容', '', '', '', 'local'))
            conn.commit()
        finally:
            conn.close()
        search_index.rebuild()
        rows = search_index.search('目标主题', workspace='lifepal', limit=1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['course_name'], '生活课程')

    def test_notes_and_tasks_are_searchable_without_rebuild(self):
        note = repo.create_workspace_item('lifepal', {'title':'晨间计划', 'body':'每天记录睡眠'})
        self.assertEqual(repo.search('睡眠', workspace='lifepal')[0]['id'], note['id'])
        self.assertEqual(repo.search('睡眠', workspace='studypal'), [])
        task = repo.create_workspace_item('workpal', {'kind':'task', 'title':'提交复盘', 'due_at':'2026-10-02'})
        self.assertTrue(any(x['title']=='提交复盘' for x in repo.list_events(workspace='workpal')))
        agenda = repo.query_agenda('2026-10-02待办', workspace='workpal')
        self.assertTrue(any(x['title']=='提交复盘' for x in agenda['items']))
        repo.update_workspace_item(task['id'], {'status':'done'})
        self.assertFalse(any(x['title']=='提交复盘' for x in repo.list_events(workspace='workpal')))

    def test_event_search_cannot_leak_into_study_even_without_course(self):
        repo.create_event({'workspace_id':repo.get_workspace('lifepal')['id'], 'title':'秘密生活', 'date':'2026-10-02'})
        self.assertEqual(search_index.search('秘密生活', workspace='studypal'), [])
        self.assertEqual(search_index.search('秘密 生', workspace='studypal'), [])

    def test_conversations_are_isolated(self):
        chat_repo.create_conversation(title='生活聊天', workspace='lifepal')
        self.assertEqual(chat_repo.list_conversations(workspace='studypal'), [])
        self.assertEqual(chat_repo.list_conversations(workspace='lifepal')[0]['title'], '生活聊天')


if __name__ == '__main__':
    unittest.main()
