import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.memory import db, domain_profiles, repo


class KnowledgeContractV2Tests(unittest.TestCase):
    def test_domain_profiles_are_loadable_and_mba_rules_are_explicit(self):
        profile = domain_profiles.profile_for_course('会计学')
        self.assertEqual(profile['id'], 'mba')
        self.assertIn('concept', profile['node_types'])
        self.assertIn('exam_point', profile['node_types'])
        self.assertIn('source_refs', profile['required_fields'])
        self.assertTrue(any('公式' in rule for rule in profile['quality_rules']))

    def test_formal_node_keeps_domain_attributes_aliases_and_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'):
                db.apply_schema()
                conn = db.connect()
                conn.execute("INSERT INTO courses(name) VALUES('会计学')")
                course_id = conn.execute("SELECT id FROM courses WHERE name='会计学'").fetchone()[0]
                conn.execute("INSERT INTO kp(course_id,name,gist) VALUES(?,?,?)", (course_id, '边际成本', '增量成本'))
                kp_id = conn.execute("SELECT id FROM kp WHERE name='边际成本'").fetchone()[0]
                source_id = conn.execute(
                    "INSERT INTO source_files(course_id,path,status) VALUES(?,?, 'indexed')",
                    (course_id, str(root / '教材.md')),
                ).lastrowid
                evidence_id = conn.execute(
                    "INSERT INTO evidence(source_id,locator_type,locator_start,locator_end,content,content_hash) VALUES(?,?,?,?,?,?)",
                    (source_id, 'markdown_line', 12, 14, 'MC = ΔTC / ΔQ', 'hash-mc'),
                ).lastrowid
                conn.commit()
                conn.close()

                result = repo.update_node_contract(kp_id, {
                    'domain': 'mba',
                    'attributes': {'formula': 'MC = ΔTC / ΔQ', 'difficulty': '综合'},
                    'aliases': ['增量成本'],
                    'evidence_ids': [evidence_id],
                })
                self.assertEqual(result['domain'], 'mba')
                node = repo.graph_of('会计学')['nodes'][0]
                self.assertEqual(node['attributes']['formula'], 'MC = ΔTC / ΔQ')
                self.assertEqual(node['aliases'], ['增量成本'])
                self.assertEqual(node['source_refs'][0]['evidence_id'], evidence_id)


if __name__ == '__main__':
    unittest.main()
