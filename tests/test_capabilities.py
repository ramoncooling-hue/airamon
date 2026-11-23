import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.hub import registry
from app.memory import calendar_sync, db, graph_tools, repo, search_index, sources


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db_root = root / 'app'
        self.db_path = self.db_root / 'test.db'
        self.patches = [patch.object(db, 'APP_DIR', self.db_root),
                        patch.object(db, 'DB_PATH', self.db_path)]
        for item in self.patches:
            item.start()
        db.apply_schema()
        conn = db.connect()
        conn.execute("INSERT INTO courses(name) VALUES('会计学')")
        self.course_id = conn.execute("SELECT id FROM courses WHERE name='会计学'").fetchone()[0]
        conn.execute("INSERT INTO kp(course_id,name,gist) VALUES(?,?,?)",
                     (self.course_id, '边际成本', '新增一单位产量的成本'))
        conn.commit()
        conn.close()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def test_fts_supports_course_shorthand_and_fallback(self):
        self.assertGreaterEqual(search_index.rebuild(), 1)
        self.assertEqual(repo.search('边际成本', '会计学')[0]['name'], '边际成本')
        self.assertEqual(repo.search('产量', 10, '会计学')[0]['name'], '边际成本')

    def test_image_and_audio_sidecar_are_extractable(self):
        root = Path(self.tmp.name)
        audio = root / 'lesson.mp3'
        audio.write_bytes(b'not-a-real-audio-file')
        audio.with_suffix('.txt').write_text('课堂转写：边际成本是新增产量的成本。', encoding='utf-8')
        text, _, evidence = sources.extract_structured(audio)
        self.assertIn('边际成本', text)
        self.assertEqual(evidence[0]['locator_type'], 'transcript')

    def test_graph_export_import_and_health(self):
        exported = graph_tools.export_graph('会计学')
        self.assertEqual(exported['schema'], 'cyberpal.graph.v1')
        report = graph_tools.health('会计学')
        self.assertTrue(report['ok'])
        conn = db.connect()
        conn.execute("INSERT INTO kp(course_id,name,kp_type) VALUES(?,?,?)",
                     (self.course_id, '第一节结构锚点', 'section'))
        conn.commit()
        conn.close()
        report = graph_tools.health('会计学')
        self.assertEqual([node['name'] for node in report['isolated_nodes']], ['边际成本'])
        self.assertEqual(len(report['structural_placeholders']), 1)
        imported = graph_tools.import_graph({
            'schema': 'cyberpal.graph.v1', 'course': '会计学',
            'nodes': [
                {'id': 1, 'course_name': '会计学', 'name': '边际成本'},
                {'id': 99, 'course_name': '会计学', 'name': '需求曲线', 'gist': '价格与需求量关系'},
            ],
            'edges': [{'from_kp': 1, 'to_kp': 99, 'kind': 'related'}],
        })
        self.assertEqual(imported['nodes_created'], 1)
        self.assertEqual(imported['edges_added'], 1)

    def test_ics_round_trip_and_conflict_is_explicit(self):
        event = repo.create_event({'course_name': '会计学', 'title': '交作业', 'category': 'hw',
                                   'date': '2026-10-08', 'source': 'ics', 'source_uid': 'uid-1'})
        ics = calendar_sync.export_ics('会计学')
        self.assertIn('UID:uid-1', ics)
        result = calendar_sync.import_ics(ics)
        self.assertEqual(result['conflicts'], 0)
        changed = ics.replace('交作业', '交最终作业')
        result = calendar_sync.import_ics(changed)
        self.assertEqual(result['conflicts'], 1)
        conflicts = calendar_sync.conflicts()
        self.assertEqual(len(conflicts), 1)
        calendar_sync.resolve_conflict(conflicts[0]['id'], 'incoming')
        self.assertEqual(repo.list_events('会计学')[0]['title'], '交最终作业')

    def test_notice_is_searchable_with_original_source(self):
        conn = db.connect()
        conn.execute(
            "INSERT INTO notices(record_id,course_name,title,body,date,source) VALUES(?,?,?,?,?,?)",
            ('notice-1', '会计学', '课程群通知：提交报告', '请在网络学堂提交报告', '2026-10-08', 'workbench/course-group'),
        )
        conn.commit()
        conn.close()
        search_index.rebuild()
        hits = repo.search('网络学堂 报告', 10, '会计学')
        self.assertTrue(hits)
        self.assertEqual(hits[0]['kind'], 'notice')
        self.assertIn('workbench/course-group', hits[0]['source'])

    def test_hub_skill_bundle_is_portable(self):
        root = Path(self.tmp.name)
        source_skills, source_agents = root / 'source-skills', root / 'source-agents'
        target_skills, target_agents = root / 'target-skills', root / 'target-agents'
        with patch.object(registry, 'SKILLS_DIR', source_skills), patch.object(registry, 'AGENTS_DIR', source_agents):
            db.apply_schema()
            registry.create_skill({'key': 'web-research', 'name': '联网检索', 'content': '检索并核对来源。'})
            bundle = registry.export_bundle()
        with patch.object(registry, 'SKILLS_DIR', target_skills), patch.object(registry, 'AGENTS_DIR', target_agents):
            result = registry.import_bundle(bundle)
            self.assertEqual(result['skills'], 1)
            self.assertTrue((target_skills / 'web-research' / 'SKILL.md').exists())


if __name__ == '__main__':
    unittest.main()
