import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.memory import db, workpal_graph


class WorkPalGraphSeedTests(unittest.TestCase):
    def test_seed_materializes_only_nodes_with_matching_evidence_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'):
                db.apply_schema()
                conn = db.connect()
                cid = conn.execute("INSERT INTO courses(name) VALUES('AI工作系统')").lastrowid
                sid = conn.execute(
                    "INSERT INTO source_files(course_id,path,status) VALUES(?,?,?)",
                    (cid, '/tmp/work.md', 'indexed'),
                ).lastrowid
                evidence_id = conn.execute(
                    """INSERT INTO evidence(source_id,locator_type,locator_start,locator_end,content,content_hash)
                       VALUES(?,?,?,?,?,?)""",
                    (sid, 'line', 1, 1,
                     'PEV闭环(Plan-Execute-Verify)+工具调用+Skill沉淀', 'work-e1'),
                ).lastrowid
                a = conn.execute("INSERT INTO kp(course_id,name) VALUES(?,?)", (cid, 'PEV闭环')).lastrowid
                b = conn.execute("INSERT INTO kp(course_id,name) VALUES(?,?)", (cid, 'L1人机协作协议')).lastrowid
                conn.execute(
                    "INSERT INTO knowledge_node_meta(kp_id,schema_version,domain,source_refs_json,status) VALUES(?,?,?,?,?)",
                    (a, 'cyberpal.knowledge.v1', 'mba', '[]', 'confirmed'),
                )
                conn.commit()
                conn.close()

                first = workpal_graph.seed()
                second = workpal_graph.seed()
                self.assertEqual(first['created'], 1)
                self.assertEqual(second['updated'], 1)
                self.assertEqual(len(first['skipped']), len(workpal_graph.WORKPAL_RELATIONS) - 1)
                conn = db.connect()
                edge = conn.execute('SELECT * FROM kp_edges').fetchone()
                meta = conn.execute('SELECT * FROM knowledge_relation_meta').fetchone()
                self.assertEqual((edge['from_kp'], edge['to_kp'], edge['kind']), (a, b, 'part_of'))
                self.assertEqual(meta['evidence_id'], evidence_id)
                self.assertGreater(meta['confidence'], 0.9)
                domains = {row['domain'] for row in conn.execute(
                    'SELECT domain FROM knowledge_node_meta WHERE kp_id IN (?,?)', (a, b)
                ).fetchall()}
                self.assertEqual(domains, {'work'})
                conn.close()


if __name__ == '__main__':
    unittest.main()

