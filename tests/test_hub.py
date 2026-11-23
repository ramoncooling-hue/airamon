import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.hub import registry
from app.memory import db


class HubRegistryTests(unittest.TestCase):
    def test_create_agent_writes_manifest_prompt_and_skill_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skills = root / 'skills' / 'research'
            skills.mkdir(parents=True)
            (skills / 'SKILL.md').write_text('# Research', encoding='utf-8')
            (skills / 'manifest.json').write_text(
                '{"key":"research","name":"Research","version":"1.0.0"}',
                encoding='utf-8',
            )
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'), \
                 patch.object(registry, 'SKILLS_DIR', root / 'skills'), patch.object(registry, 'AGENTS_DIR', root / 'agents'):
                db.apply_schema()
                registry.sync()
                created = registry.create_agent({
                    'key': 'web-helper',
                    'name': '联网助理',
                    'version': '1.0.0',
                    'description': '检索公开网页并整理来源',
                    'skills': ['research'],
                    'content': '保持自然对话，并在需要时检索网页。',
                })
                self.assertEqual(created['key'], 'web-helper')
                self.assertEqual(created['skills'], ['research'])
                self.assertEqual((root / 'agents' / 'web-helper' / 'system.md').read_text(encoding='utf-8'),
                                 '保持自然对话，并在需要时检索网页。')
                self.assertEqual(json.loads((root / 'agents' / 'web-helper' / 'manifest.json').read_text(encoding='utf-8'))['name'], '联网助理')
                conn = db.connect()
                try:
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM agent_skills').fetchone()[0], 1)
                finally:
                    conn.close()

    def test_create_agent_rejects_duplicate_and_unknown_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agents = root / 'agents' / 'demo'
            agents.mkdir(parents=True)
            (agents / 'system.md').write_text('# Demo', encoding='utf-8')
            (agents / 'manifest.json').write_text(
                '{"key":"demo","name":"Demo","version":"1.0.0"}', encoding='utf-8'
            )
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'), \
                 patch.object(registry, 'SKILLS_DIR', root / 'skills'), patch.object(registry, 'AGENTS_DIR', root / 'agents'):
                db.apply_schema()
                registry.sync()
                with self.assertRaisesRegex(ValueError, '已存在'):
                    registry.create_agent({'key': 'demo', 'name': 'Other', 'version': '1.0.0', 'content': 'x'})
                with self.assertRaisesRegex(ValueError, '技能不存在'):
                    registry.create_agent({'key': 'new-agent', 'name': 'New', 'version': '1.0.0',
                                           'skills': ['missing'], 'content': 'x'})

    def test_agent_cannot_bind_disabled_skill_and_public_skill_has_usage_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'), \
                 patch.object(registry, 'SKILLS_DIR', root / 'skills'), patch.object(registry, 'AGENTS_DIR', root / 'agents'):
                db.apply_schema()
                registry.sync()
                registry.create_skill({'key': 'draft-skill', 'name': 'Draft Skill', 'content': '# Draft'})
                registry.set_enabled('skill', 'draft-skill', False)
                with self.assertRaisesRegex(ValueError, '技能未启用'):
                    registry.create_agent({'key': 'draft-agent', 'name': 'Draft Agent', 'version': '1.0.0',
                                           'skills': ['draft-skill'], 'content': '# Agent'})
                public = registry.list_registry()
                skill = next(item for item in public['skills'] if item['key'] == 'draft-skill')
                self.assertEqual(skill['bound_agent_count'], 0)
                self.assertEqual(skill['revision_count'], 0)

    def test_skill_detail_reports_usage_and_revision_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'), \
                 patch.object(registry, 'SKILLS_DIR', root / 'skills'), patch.object(registry, 'AGENTS_DIR', root / 'agents'):
                db.apply_schema()
                registry.sync()
                registry.create_skill({'key': 'tracked', 'name': 'Tracked', 'content': '# One'})
                registry.create_agent({'key': 'reader', 'name': 'Reader', 'version': '1.0.0',
                                       'skills': ['tracked'], 'content': '# Reader'})
                registry.update('skill', 'tracked', {'version': '1.1.0', 'content': '# Two'})
                detail = registry.get_detail('skill', 'tracked')
                self.assertEqual(detail['bound_agent_count'], 1)
                self.assertEqual(detail['revision_count'], 1)

    def test_editing_existing_agent_can_keep_a_skill_that_was_later_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'), \
                 patch.object(registry, 'SKILLS_DIR', root / 'skills'), patch.object(registry, 'AGENTS_DIR', root / 'agents'):
                db.apply_schema()
                registry.sync()
                registry.create_skill({'key': 'legacy', 'name': 'Legacy', 'content': '# Legacy'})
                registry.create_agent({'key': 'legacy-agent', 'name': 'Legacy Agent', 'version': '1.0.0',
                                       'skills': ['legacy'], 'content': '# Agent'})
                registry.set_enabled('skill', 'legacy', False)
                updated = registry.update('agent', 'legacy-agent', {
                    'name': 'Renamed Agent', 'skills': ['legacy'], 'content': '# Updated'
                })
                self.assertEqual(updated['name'], 'Renamed Agent')
                self.assertEqual(updated['skills'], ['legacy'])

    def test_syncs_file_backed_agent_and_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skills = root / 'skills' / 'demo'
            agents = root / 'agents' / 'demo'
            skills.mkdir(parents=True)
            agents.mkdir(parents=True)
            (skills / 'SKILL.md').write_text('# Demo', encoding='utf-8')
            (skills / 'manifest.json').write_text('{"key":"demo","name":"Demo Skill","version":"1.0.0"}', encoding='utf-8')
            (agents / 'system.md').write_text('# Demo Agent', encoding='utf-8')
            (agents / 'manifest.json').write_text('{"key":"demo","name":"Demo Agent","version":"1.0.0","skills":["demo"]}', encoding='utf-8')
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'), \
                 patch.object(registry, 'SKILLS_DIR', root / 'skills'), patch.object(registry, 'AGENTS_DIR', root / 'agents'):
                db.apply_schema()
                result = registry.sync()
                self.assertEqual(result['skills'][0]['key'], 'demo')
                self.assertEqual(result['agents'][0]['key'], 'demo')
                public = registry.list_registry()
                self.assertNotIn('path', public['skills'][0])
                self.assertEqual(public['agents'][0]['skills'], ['demo'])
                conn = db.connect()
                try:
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM agent_skills').fetchone()[0], 1)
                finally:
                    conn.close()
                disabled = registry.set_enabled('skill', 'demo', False)
                self.assertEqual(disabled['enabled'], False)
                self.assertNotIn('path', disabled)
                self.assertNotIn('manifest_json', disabled)

    def test_detail_and_update_replace_only_registered_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skills = root / 'skills' / 'demo'
            agents = root / 'agents' / 'demo'
            skills.mkdir(parents=True)
            agents.mkdir(parents=True)
            (skills / 'SKILL.md').write_text('# Before', encoding='utf-8')
            (skills / 'manifest.json').write_text('{"key":"demo","name":"Demo Skill","version":"1.0.0"}', encoding='utf-8')
            (agents / 'system.md').write_text('# Agent', encoding='utf-8')
            (agents / 'manifest.json').write_text('{"key":"demo","name":"Demo Agent","version":"1.0.0","skills":["demo"]}', encoding='utf-8')
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'), \
                 patch.object(registry, 'SKILLS_DIR', root / 'skills'), patch.object(registry, 'AGENTS_DIR', root / 'agents'):
                db.apply_schema()
                registry.sync()
                detail = registry.get_detail('skill', 'demo')
                self.assertEqual(detail['content'], '# Before')
                updated = registry.update('skill', 'demo', {
                    'name': 'Updated Skill', 'version': '1.1.0',
                    'description': 'Updated description', 'content': '# After'
                })
                self.assertEqual(updated['version'], '1.1.0')
                self.assertEqual((skills / 'SKILL.md').read_text(encoding='utf-8'), '# After')
                manifest = json.loads((skills / 'manifest.json').read_text(encoding='utf-8'))
                self.assertEqual(manifest['name'], 'Updated Skill')
                self.assertEqual(manifest['description'], 'Updated description')
                self.assertEqual(registry.get_detail('skill', 'demo')['content'], '# After')
                revisions = registry.list_revisions('skill', 'demo')
                self.assertEqual(len(revisions), 1)
                self.assertEqual(revisions[0]['content'], '# Before')
                restored = registry.restore_revision(revisions[0]['id'])
                self.assertEqual(restored['content'], '# Before')
                self.assertEqual(restored['version'], '1.0.0')

    def test_invalid_kind_and_path_like_key_are_rejected(self):
        with self.assertRaises(ValueError):
            registry.get_detail('file', 'demo')
        with self.assertRaises(ValueError):
            registry.get_detail('skill', '../demo')


if __name__ == '__main__':
    unittest.main()
