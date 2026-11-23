"""T2.21: knowledge documents HTTP API, wikilinks and kp create/soft delete."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.api import routes
from app.memory import db, documents, repo, wikilinks


class KnowledgeApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [
            patch.object(db, 'APP_DIR', self.root / 'app'),
            patch.object(db, 'DB_PATH', self.root / 'app' / 'api.db'),
        ]
        for item in self.patches:
            item.start()
        db.apply_schema()
        conn = db.connect()
        try:
            with conn:
                conn.execute("INSERT INTO courses(name) VALUES('会计学')")
                conn.execute(
                    "UPDATE courses SET workspace_id=(SELECT id FROM workspaces WHERE key='studypal') "
                    "WHERE name='会计学'")
        finally:
            conn.close()
        self.course_id = self._scalar("SELECT id FROM courses WHERE name='会计学'")

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def _scalar(self, sql, params=()):
        conn = db.connect()
        try:
            return conn.execute(sql, params).fetchone()[0]
        finally:
            conn.close()

    def _create(self, title='边际成本', body='# 边际成本\n\n新增产量的成本。', **kwargs):
        return routes.documents_create(routes.DocumentIn(title=title, body_md=body, **kwargs))

    def _status(self, exc_info):
        return exc_info.exception.status_code

    def test_crud_revisions_soft_delete_restore_and_rollback(self):
        created = self._create(tags=['经济学', '成本'])
        document_id = created['id']
        self.assertEqual(created['version'], 1)
        self.assertEqual(set(created['tags']), {'经济学', '成本'})

        detail = routes.documents_detail(document_id)
        self.assertEqual(detail['document']['title'], '边际成本')
        self.assertEqual(detail['revisions_count'], 1)
        self.assertEqual(set(detail['tags']), {'经济学', '成本'})

        updated = routes.documents_patch(
            document_id, routes.DocumentPatchIn(title='边际成本（改）', body_md='v2', expected_version=1))
        self.assertEqual(updated['version'], 2)
        revisions = routes.documents_revisions(document_id)
        self.assertEqual([item['version'] for item in revisions], [2, 1])

        with self.assertRaises(HTTPException) as stale:
            routes.documents_patch(document_id, routes.DocumentPatchIn(body_md='v3', expected_version=1))
        self.assertEqual(self._status(stale), 409)

        deleted = routes.documents_delete(document_id)
        self.assertEqual(deleted['document']['status'], 'deleted')
        self.assertEqual(routes.documents_list(workspace='studypal'), [])
        with self.assertRaises(HTTPException) as missing:
            routes.documents_detail(document_id)
        self.assertEqual(self._status(missing), 404)
        self.assertEqual(len(routes.documents_list(workspace='studypal', status='all')), 1)

        restored = routes.documents_restore(document_id)
        self.assertEqual(restored['document']['status'], 'active')
        rolled = routes.documents_restore_revision(document_id, 1)
        self.assertEqual(rolled['document']['version'], 3)
        self.assertEqual(rolled['document']['body_md'], '# 边际成本\n\n新增产量的成本。')
        self.assertEqual(routes.documents_revisions(document_id)[0]['origin'], 'rollback:v1')

    def test_validation_limits_and_not_found(self):
        with self.assertRaises(HTTPException) as too_long:
            routes.documents_create(routes.DocumentIn(title='标题', body_md='x' * 200001))
        self.assertEqual(self._status(too_long), 400)
        with self.assertRaises(HTTPException) as too_many_tags:
            routes.documents_create(routes.DocumentIn(
                title='标题', tags=[f'tag{index}' for index in range(21)]))
        self.assertEqual(self._status(too_many_tags), 400)
        with self.assertRaises(HTTPException) as long_tag:
            routes.documents_create(routes.DocumentIn(title='标题', tags=['x' * 41]))
        self.assertEqual(self._status(long_tag), 400)
        with self.assertRaises(HTTPException) as bad_kind:
            routes.documents_create(routes.DocumentIn(title='标题', kind='bad'))
        self.assertEqual(self._status(bad_kind), 400)
        with self.assertRaises(HTTPException) as bad_workspace:
            routes.documents_create(routes.DocumentIn(title='标题', workspace_key='nope'))
        self.assertEqual(self._status(bad_workspace), 404)
        with self.assertRaises(HTTPException) as missing:
            routes.documents_detail(9999)
        self.assertEqual(self._status(missing), 404)
        with self.assertRaises(HTTPException) as bad_patch:
            routes.documents_patch(9999, routes.DocumentPatchIn(title='不存在'))
        self.assertEqual(self._status(bad_patch), 404)

    def test_tags_and_workspace_isolation(self):
        first = self._create(title='文档一', tags=['经济', '成本'])
        self._create(title='文档二', tags=['成本'])
        self.assertEqual(routes.documents_patch(first['id'], routes.DocumentPatchIn(tags=['经济']))
                         ['tags'], ['经济'])
        tags = {item['name']: item['document_count'] for item in routes.tags_list('studypal')}
        self.assertEqual(tags, {'成本': 1, '经济': 1})
        other = self._create(title='工作文档', workspace_key='workpal')
        self.assertEqual(other['workspace_id'], self._scalar(
            "SELECT id FROM workspaces WHERE key='workpal'"))
        self.assertEqual({item['id'] for item in routes.documents_list(workspace='workpal')},
                         {other['id']})
        self.assertNotIn(other['id'], {item['id'] for item in routes.documents_list(workspace='studypal')})
        self.assertEqual(routes.documents_lookup('studypal', '工作文档')['ids'], {})
        self.assertEqual(routes.documents_lookup('workpal', '工作文档')['ids'],
                         {'工作文档': other['id']})

    def test_wikilink_parse_resolve_backlinks_and_bad_links(self):
        tick = chr(96)
        fence = tick * 3
        body = ('[[标题]] [[标题|显示名]] ' + tick + '[[代码里]]' + tick
                + '\n' + fence + '\n[[围栏]]\n' + fence)
        parsed = wikilinks.parse_wikilinks(body)
        self.assertEqual([item['target'] for item in parsed], ['标题', '标题'])
        self.assertEqual([item['label'] for item in parsed], ['标题', '显示名'])
        self.assertEqual((parsed[0]['start'], parsed[0]['end']), (0, 6))

        link_target = self._create(title='目标文档', body='目标正文')
        source = self._create(title='来源文档', body='参见 [[目标文档]] 与 [[坏链目标]]')
        links = routes.documents_detail(link_target['id'])
        self.assertEqual(len(links['backlinks']), 1)
        self.assertEqual(links['backlinks'][0]['document_id'], source['id'])
        self.assertEqual(links['backlinks'][0]['link_text'], '目标文档')
        self.assertIn('参见', links['backlinks'][0]['context'])
        self.assertEqual(links['bad_links'], [])

        bad = routes.documents_backlinks(source['id'])
        self.assertEqual(bad['bad_links'], [{'target': '坏链目标', 'reason': 'missing'}])
        looked = routes.documents_lookup('studypal', '目标文档,坏链目标')
        self.assertEqual(looked['ids'], {'目标文档': link_target['id']})
        self.assertEqual(looked['missing'], ['坏链目标'])

        # kp-only targets are known but have no document yet.
        kp = routes.kp_create(routes.KpCreateIn(course='会计学', name='概念节点'))
        self._create(title='指向概念', body='参见 [[概念节点]]')
        lookup = routes.documents_lookup('studypal', '概念节点')
        self.assertEqual(lookup['kp_ids'], {'概念节点': kp['kp']['id']})
        self.assertEqual(lookup['missing'], [])

    def test_lookup_route_is_registered_before_document_id(self):
        paths = [getattr(route, 'path', '') for route in routes.router.routes]
        self.assertLess(paths.index('/api/documents/lookup'),
                        paths.index('/api/documents/{document_id}'))

    def test_kp_create_soft_delete_and_revive(self):
        created = routes.kp_create(routes.KpCreateIn(
            course='会计学', name='边际成本', gist='一句话', detail='详解'))
        kp_id = created['kp']['id']
        self.assertEqual(created['kp']['gist'], '一句话')
        self.assertEqual(repo.get_kp(kp_id)['name'], '边际成本')
        self.assertEqual(self._scalar(
            'SELECT COUNT(*) FROM kp_identities WHERE kp_id=?', (kp_id,)), 1)
        self.assertIn(kp_id, {item['id'] for item in repo.list_kp(course='会计学')})

        with self.assertRaises(HTTPException):
            routes.kp_create(routes.KpCreateIn(course='会计学', name='边际成本'))
        deleted = routes.kp_delete(kp_id)
        self.assertTrue(deleted['deleted'])
        self.assertIsNone(repo.get_kp(kp_id))
        self.assertNotIn(kp_id, {item['id'] for item in repo.list_kp(course='会计学')})
        self.assertNotIn(kp_id, {node['id'] for node in repo.graph_of('会计学')['nodes']})
        with self.assertRaises(HTTPException):
            routes.kp_delete(kp_id)

        revived = routes.kp_create(routes.KpCreateIn(course='会计学', name='边际成本', gist='重启'))
        self.assertEqual(revived['kp']['id'], kp_id)
        self.assertEqual(revived['kp']['gist'], '重启')
        self.assertIsNotNone(repo.get_kp(kp_id))


if __name__ == '__main__':
    unittest.main()
