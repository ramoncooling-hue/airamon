"""T3.11 / O-KB-6: Markdown vault export (files, frontmatter, manifest)."""

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.api import routes
from app.memory import db, documents, exporter

try:  # Optional strict YAML check; the dependency list has no PyYAML requirement.
    import yaml
except ImportError:  # pragma: no cover - exercised only in lean environments
    yaml = None

TIMESTAMP_DIR = re.compile(r'^knowledge-\d{8}-\d{6}(-\d+)?$')
_QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"|[^,]+')


def _split_frontmatter(text):
    if not text.startswith('---\n'):
        raise AssertionError('frontmatter must start with ---')
    remainder = text[4:]
    end = remainder.find('\n---\n')
    if end < 0:
        raise AssertionError('frontmatter must end with ---')
    return remainder[:end], remainder[end + 5:]


def _frontmatter_fields(block):
    fields = {}
    for line in block.splitlines():
        key, _, value = line.partition(':')
        value = value.strip()
        if key == 'tags':
            items = []
            inner = value[1:-1].strip() if value.startswith('[') else ''
            if inner:
                for item in _QUOTED.findall(inner):
                    item = item.strip()
                    items.append(_unquote(item) if item.startswith('"') else item)
            fields['tags'] = items
        elif value.startswith('"'):
            fields[key] = _unquote(value)
        elif value == 'null':
            fields[key] = None
        else:
            fields[key] = value
    return fields


def _unquote(value):
    if value.startswith('"') and value.endswith('"'):
        return json.loads(value)
    return value


class KnowledgeExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / 'data'
        self.patches = [
            patch.object(db, 'DATA_DIR', self.data),
            patch.object(db, 'APP_DIR', self.data / 'app'),
            patch.object(db, 'DB_PATH', self.data / 'app' / 'export.db'),
        ]
        for item in self.patches:
            item.start()
        db.apply_schema()
        conn = db.connect()
        try:
            with conn:
                conn.execute("INSERT INTO courses(name) VALUES('会计学')")
                conn.execute(
                    "UPDATE courses SET workspace_id="
                    "(SELECT id FROM workspaces WHERE key='studypal') WHERE name='会计学'")
                conn.execute(
                    "INSERT INTO kp(course_id,name) VALUES("
                    "(SELECT id FROM courses WHERE name='会计学'),'边际成本')")
        finally:
            conn.close()
        self.course_id = self._scalar("SELECT id FROM courses WHERE name='会计学'")
        self.kp_id = self._scalar("SELECT id FROM kp WHERE name='边际成本'")

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

    def _create(self, title, body='# 标题\n\n正文。', **kwargs):
        values = {'course_id': self.course_id}
        values.update(kwargs)
        return documents.create('studypal', title=title, body_md=body, **values)

    def _read(self, export_dir, relative):
        return (Path(export_dir) / relative).read_text(encoding='utf-8')

    def test_export_writes_vault_structure_manifest_and_verbatim_body(self):
        body = '# 成本\n\n关联 [[边际成本]]，参见 [[机会成本|机会]]。\n'
        self._create('成本笔记', body, tags=['经济学', '成本'])
        result = exporter.export_knowledge('studypal')

        self.assertTrue(result['ok'])
        export_dir = Path(result['dir'])
        self.assertEqual(export_dir.parent, (self.data / 'exports').resolve())
        self.assertRegex(export_dir.name, TIMESTAMP_DIR)
        self.assertEqual(result['files'], ['成本笔记.md'])
        self.assertEqual(result['skipped'], 0)

        text = self._read(export_dir, '成本笔记.md')
        self.assertTrue(text.startswith('---\n'))
        self.assertTrue(text.endswith(body))
        self.assertIn('[[边际成本]]', text)
        self.assertIn('[[机会成本|机会]]', text)

        manifest = json.loads(self._read(export_dir, exporter.MANIFEST_NAME))
        self.assertEqual(manifest['format'], 'mindock.knowledge.v1')
        self.assertEqual(manifest['workspace'], 'studypal')
        self.assertEqual(manifest['skipped'], 0)
        self.assertEqual([item['file'] for item in manifest['documents']], ['成本笔记.md'])

    def test_frontmatter_is_yaml_parseable_with_expected_fields(self):
        title = '带: 冒号 与 "引号" 的标题'
        document = self._create(title, '# 正文\n', tags=['经济学', '成本'],
                                kind='concept', kp_id=self.kp_id)
        result = exporter.export_knowledge('studypal')
        rel = result['files'][0]
        text = self._read(result['dir'], rel)
        block, body = _split_frontmatter(text)
        fields = _frontmatter_fields(block)

        self.assertEqual(fields['title'], title)
        self.assertEqual(fields['workspace'], 'studypal')
        self.assertEqual(fields['course'], '会计学')
        self.assertEqual(fields['tags'], sorted(['经济学', '成本']))
        self.assertEqual(fields['kind'], 'concept')
        self.assertEqual(fields['kp_id'], str(self.kp_id))
        self.assertTrue(fields['created'])
        self.assertTrue(fields['updated'])
        self.assertEqual(body, '# 正文\n')

        if yaml is not None:
            parsed = yaml.safe_load(block)
            self.assertIsInstance(parsed, dict)
            self.assertEqual(parsed['title'], title)
            self.assertEqual(parsed['tags'], sorted(['经济学', '成本']))
            self.assertEqual(parsed['kp_id'], self.kp_id)
            self.assertEqual(parsed['kind'], 'concept')
            self.assertEqual(parsed['course'], '会计学')

    def test_duplicate_titles_get_numeric_suffixes(self):
        for _ in range(3):
            self._create('同名笔记', '正文')
        result = exporter.export_knowledge('studypal')
        self.assertEqual(result['files'], ['同名笔记.md', '同名笔记-2.md', '同名笔记-3.md'])
        for relative in result['files']:
            self.assertTrue((Path(result['dir']) / relative).is_file())

    def test_dangerous_titles_are_sanitized_and_stay_inside_export_dir(self):
        self._create('危险/名称\\带: 星*问? 引"号<尖>|竖')
        self._create('..')
        result = exporter.export_knowledge('studypal')
        export_dir = Path(result['dir']).resolve()
        self.assertEqual(len(result['files']), 2)
        for relative in result['files']:
            self.assertNotIn('\\', relative)
            for illegal in '/:*?"<>|':
                self.assertNotIn(illegal, relative)
            self.assertNotIn('..', relative)
            self.assertTrue(relative.endswith('.md'))
            self.assertEqual((export_dir / relative).resolve().parent, export_dir)
        block, _ = _split_frontmatter(self._read(result['dir'], result['files'][0]))
        self.assertEqual(_frontmatter_fields(block)['title'], '危险/名称\\带: 星*问? 引"号<尖>|竖')

    def test_dest_dir_outside_data_directory_is_rejected(self):
        outside = self.root / 'outside-vault'
        with self.assertRaises(ValueError) as raised:
            exporter.export_knowledge('studypal', dest_dir=str(outside))
        self.assertIn('数据目录', str(raised.exception))
        with self.assertRaises(ValueError):
            exporter.export_knowledge('studypal', dest_dir='../outside-vault')
        with self.assertRaises(HTTPException) as http_error:
            routes.knowledge_export(
                routes.KnowledgeExportIn(workspace_key='studypal', dest_dir=str(outside)))
        self.assertEqual(http_error.exception.status_code, 400)

        link = self.data / 'escape-link'
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            link = None
        if link is not None:
            with self.assertRaises(ValueError):
                exporter.export_knowledge('studypal', dest_dir=str(link))

        inside = self.data / 'vaults'
        accepted = exporter.export_knowledge('studypal', dest_dir=str(inside))
        self.assertEqual(Path(accepted['dir']).parent, inside.resolve())
        nested = exporter.export_knowledge('studypal', dest_dir='vaults/nested')
        self.assertEqual(Path(nested['dir']).parent, (inside / 'nested').resolve())

    def test_empty_library_returns_empty_files_and_empty_manifest(self):
        result = exporter.export_knowledge('workpal')
        self.assertTrue(result['ok'])
        self.assertEqual(result['files'], [])
        self.assertEqual(result['skipped'], 0)
        manifest = json.loads(self._read(result['dir'], exporter.MANIFEST_NAME))
        self.assertEqual(manifest['documents'], [])

    def test_archived_documents_go_to_subdirectory_and_deleted_are_skipped(self):
        self._create('现行笔记')
        archived = self._create('旧版笔记')
        documents.update(archived['id'], status='archived')
        deleted = self._create('已删笔记')
        documents.soft_delete(deleted['id'])

        result = exporter.export_knowledge('studypal')
        self.assertEqual(result['skipped'], 1)
        self.assertEqual(sorted(result['files']), ['archived/旧版笔记.md', '现行笔记.md'])
        self.assertTrue((Path(result['dir']) / 'archived').is_dir())
        self.assertFalse((Path(result['dir']) / '已删笔记.md').exists())
        manifest = json.loads(self._read(result['dir'], exporter.MANIFEST_NAME))
        self.assertEqual(manifest['skipped'], 1)
        self.assertEqual(sorted(item['status'] for item in manifest['documents']),
                         ['active', 'archived'])

    def test_api_endpoint_returns_contract_and_rejects_unknown_workspace(self):
        self._create('接口笔记')
        response = routes.knowledge_export(routes.KnowledgeExportIn(workspace_key='studypal'))
        self.assertEqual(set(response), {'ok', 'dir', 'files', 'skipped'})
        self.assertTrue(response['ok'])
        self.assertEqual(response['files'], ['接口笔记.md'])
        self.assertEqual(Path(response['dir']).parent, (self.data / 'exports').resolve())
        with self.assertRaises(HTTPException) as missing:
            routes.knowledge_export(routes.KnowledgeExportIn(workspace_key='missing'))
        self.assertEqual(missing.exception.status_code, 400)


if __name__ == '__main__':
    unittest.main()
