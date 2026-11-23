"""T2.20: document repository CRUD, revisions, soft delete, tags, legacy kp docs."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.memory import db, documents


class DocumentsModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [
            patch.object(db, 'APP_DIR', self.root / 'app'),
            patch.object(db, 'DB_PATH', self.root / 'app' / 'documents.db'),
        ]
        for item in self.patches:
            item.start()
        db.apply_schema()
        conn = db.connect()
        try:
            with conn:
                conn.execute("INSERT INTO courses(name) VALUES('会计学')")
                conn.execute("INSERT INTO courses(name) VALUES('组织行为学')")
                conn.execute(
                    "UPDATE courses SET workspace_id=(SELECT id FROM workspaces WHERE key='studypal') "
                    "WHERE name='会计学'")
                conn.execute(
                    "UPDATE courses SET workspace_id=(SELECT id FROM workspaces WHERE key='workpal') "
                    "WHERE name='组织行为学'")
                conn.execute("INSERT INTO kp(course_id,name) VALUES("
                             "(SELECT id FROM courses WHERE name='会计学'),'边际成本')")
        finally:
            conn.close()
        self.course_id = self._scalar("SELECT id FROM courses WHERE name='会计学'")
        self.other_course_id = self._scalar("SELECT id FROM courses WHERE name='组织行为学'")
        self.kp_id = self._scalar("SELECT id FROM kp WHERE name='边际成本'")

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def _query(self, sql, params=()):
        conn = db.connect()
        try:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def _scalar(self, sql, params=()):
        conn = db.connect()
        try:
            return conn.execute(sql, params).fetchone()[0]
        finally:
            conn.close()

    def _workspace(self, key):
        return self._scalar('SELECT id FROM workspaces WHERE key=?', (key,))

    def test_create_get_list_and_validation(self):
        doc = documents.create(
            'studypal', title='边际成本', body_md='# 边际成本\n\n新增产量的成本。',
            tags=['经济学', '成本'])
        self.assertEqual(doc['version'], 1)
        self.assertEqual(doc['status'], 'active')
        self.assertEqual(doc['kind'], 'note')
        self.assertIsNone(doc['course_id'])
        self.assertIsNone(doc['kp_id'])
        self.assertEqual(set(doc['tags']), {'经济学', '成本'})
        self.assertEqual(self._workspace('studypal'), doc['workspace_id'])

        fetched = documents.get(doc['id'])
        self.assertEqual(fetched['title'], '边际成本')
        self.assertEqual(fetched['body_md'], '# 边际成本\n\n新增产量的成本。')
        listed = documents.list(workspace='studypal')
        self.assertEqual([item['id'] for item in listed], [doc['id']])
        self.assertEqual([item['id'] for item in documents.list(workspace='studypal', q='新增产量')],
                         [doc['id']])
        self.assertEqual(documents.list(workspace='workpal'), [])
        self.assertIsNone(documents.get(9999))

        with self.assertRaises(ValueError):
            documents.create('studypal', title='   ')
        with self.assertRaises(ValueError):
            documents.create('studypal', title='标题', kind='bad')
        with self.assertRaises(ValueError):
            documents.create('nope', title='标题')
        with self.assertRaises(ValueError):
            documents.create('studypal', title='标题', course_id=9999)
        with self.assertRaises(ValueError):
            documents.create('studypal', title='标题', course_id=self.other_course_id)
        with self.assertRaises(ValueError):
            documents.create('studypal', title='标题',
                             course_id=self.other_course_id, kp_id=self.kp_id)
        self.assertEqual(self._scalar('SELECT COUNT(*) FROM tags'), 2)
        self.assertEqual(self._scalar('SELECT COUNT(*) FROM entity_tags'), 2)

    def test_update_versions_and_expected_version_conflict(self):
        doc = documents.create('studypal', title='边际成本', body_md='v1')
        updated = documents.update(
            doc['id'], title='边际成本（改）', body_md='v2', expected_version=1, origin='manual-edit')
        self.assertEqual(updated['version'], 2)
        self.assertEqual(updated['title'], '边际成本（改）')
        self.assertEqual(updated['body_md'], 'v2')
        revisions = documents.list_revisions(doc['id'])
        self.assertEqual([item['version'] for item in revisions], [2, 1])
        self.assertEqual(revisions[1]['body_md'], 'v1')
        self.assertEqual(revisions[0]['origin'], 'manual-edit')

        with self.assertRaises(ValueError) as caught:
            documents.update(doc['id'], body_md='v3', expected_version=1)
        self.assertIn('冲突', str(caught.exception))

        # Tags-only patches keep the document version and revision count.
        tagged = documents.update(doc['id'], tags=['重点'])
        self.assertEqual(tagged['version'], 2)
        self.assertEqual(tagged['tags'], ['重点'])
        self.assertEqual(len(documents.list_revisions(doc['id'])), 2)
        unchanged = documents.update(doc['id'], title=tagged['title'])
        self.assertEqual(unchanged['version'], 2)

    def test_soft_delete_restore_and_restore_revision(self):
        doc = documents.create('studypal', title='边际成本', body_md='v1')
        documents.update(doc['id'], body_md='v2', expected_version=1)
        deleted = documents.soft_delete(doc['id'])
        self.assertEqual(deleted['status'], 'deleted')
        self.assertEqual(deleted['version'], 2)
        self.assertEqual(self._scalar('SELECT COUNT(*) FROM knowledge_documents'), 1)
        self.assertEqual([item['id'] for item in documents.list(workspace='studypal')], [])
        self.assertIn(doc['id'], [item['id'] for item in documents.list(
            workspace='studypal', status='all')])
        with self.assertRaises(ValueError):
            documents.update(doc['id'], body_md='v3')

        restored = documents.restore(doc['id'])
        self.assertEqual(restored['status'], 'active')
        rolled = documents.restore_revision(doc['id'], 1)
        self.assertEqual(rolled['version'], 3)
        self.assertEqual(rolled['body_md'], 'v1')
        versions = [item['version'] for item in documents.list_revisions(doc['id'])]
        self.assertEqual(versions, [3, 2, 1])
        self.assertEqual(documents.list_revisions(doc['id'])[0]['origin'], 'rollback:v1')
        with self.assertRaises(ValueError):
            documents.restore_revision(doc['id'], 99)

    def test_tags_are_workspace_scoped_and_reused(self):
        one = documents.create('studypal', title='文档一', tags=['经济学', '成本'])
        two = documents.create('studypal', title='文档二', tags=['成本'])
        self.assertEqual(two['tags'], ['成本'])
        self.assertEqual({row['name'] for row in self._query('SELECT name FROM tags')},
                         {'经济学', '成本'})
        self.assertEqual(self._scalar(
            "SELECT COUNT(*) FROM entity_tags WHERE entity_type='document'"), 3)
        listed = {row['name']: row['document_count'] for row in documents.list_tags('studypal')}
        self.assertEqual(listed, {'成本': 2, '经济学': 1})
        self.assertEqual(documents.list_tags('workpal'), [])

        self.assertEqual(documents.set_tags(one['id'], ['重点']), ['重点'])
        self.assertEqual(documents.get_tags(one['id']), ['重点'])
        listed = {row['name']: row['document_count'] for row in documents.list_tags('studypal')}
        self.assertEqual(listed, {'成本': 1, '经济学': 0, '重点': 1})
        with self.assertRaises(ValueError):
            documents.set_tags(one['id'], [1])
        with self.assertRaises(ValueError):
            documents.set_tags(one['id'], ['x' * 41])

    def test_kp_link_is_unique_and_deleted_kp_is_rejected(self):
        doc = documents.create('studypal', title='边际成本', kp_id=self.kp_id)
        self.assertEqual(doc['course_id'], self.course_id)
        self.assertEqual(documents.find_by_kp(self.kp_id)['id'], doc['id'])
        with self.assertRaises(ValueError) as caught:
            documents.create('studypal', title='重复文档', kp_id=self.kp_id)
        self.assertIn('已有关联文档', str(caught.exception))

        conn = db.connect()
        try:
            with conn:
                conn.execute("UPDATE kp SET deleted_at='2026-10-17 00:00:00' WHERE id=?",
                             (self.kp_id,))
        finally:
            conn.close()
        with self.assertRaises(ValueError):
            documents.create('studypal', title='删除后关联', kp_id=self.kp_id)

    def test_archived_status_and_course_workspace_validation(self):
        doc = documents.create('studypal', title='归档文档', body_md='正文')
        archived = documents.update(doc['id'], status='archived')
        self.assertEqual(archived['status'], 'archived')
        self.assertEqual(archived['version'], 2)
        self.assertEqual([item['id'] for item in documents.list(workspace='studypal')], [])
        self.assertIn(doc['id'], [item['id'] for item in documents.list(
            workspace='studypal', status='archived')])
        self.assertIn(doc['id'], [item['id'] for item in documents.list(
            workspace='studypal', status='all')])
        with self.assertRaises(ValueError):
            documents.update(doc['id'], status='deleted')

        with self.assertRaises(ValueError):
            documents.create('studypal', title='跨工作区课程', course_id=self.other_course_id)
        other = documents.create('workpal', title='组织笔记', course_id=self.other_course_id)
        self.assertEqual(other['workspace_id'], self._workspace('workpal'))


if __name__ == '__main__':
    unittest.main()
