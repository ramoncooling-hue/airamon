import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.memory import db, sources


class CandidateGovernanceTests(unittest.TestCase):
    def test_duplicate_candidate_is_not_reported_as_new_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'):
                db.apply_schema()
                conn = db.connect()
                conn.execute("INSERT INTO courses(name) VALUES('测试课程')")
                course_id = conn.execute("SELECT id FROM courses WHERE name='测试课程'").fetchone()[0]
                conn.execute("INSERT INTO kp(course_id,name) VALUES(?,?)", (course_id, '已有概念'))
                conn.execute("INSERT INTO source_files(course_id,path,status) VALUES(?,?,?)", (course_id, '/tmp/x.md', 'indexed'))
                source_id = conn.execute('SELECT id FROM source_files').fetchone()[0]
                conn.execute("INSERT INTO knowledge_candidates(source_id,schema_version,name) VALUES(?,?,?)", (source_id, 'cyberpal.knowledge.v1', '已有概念'))
                conn.commit()
                candidate_id = conn.execute('SELECT id FROM knowledge_candidates').fetchone()[0]
                conn.close()
                result = sources.confirm_candidate(candidate_id)
                self.assertEqual(result['status'], 'duplicate')
                self.assertIsNotNone(result['target_kp_id'])
                conn = db.connect()
                try:
                    self.assertEqual(conn.execute('SELECT status FROM knowledge_candidates').fetchone()[0], 'duplicate')
                finally:
                    conn.close()

    def test_confirmed_nodes_without_shared_evidence_do_not_create_relation_noise(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'):
                db.apply_schema()
                conn = db.connect()
                conn.execute("INSERT INTO courses(name) VALUES('测试课程')")
                course_id = conn.execute("SELECT id FROM courses").fetchone()[0]
                source_id = conn.execute(
                    "INSERT INTO source_files(course_id,path,status) VALUES(?,?,?)",
                    (course_id, '/tmp/book.md', 'indexed'),
                ).lastrowid
                evidence_a = conn.execute(
                    "INSERT INTO evidence(source_id,locator_type,locator_start,locator_end,content,content_hash) VALUES(?,?,?,?,?,?)",
                    (source_id, 'line', 1, 1, '甲', 'a'),
                ).lastrowid
                evidence_b = conn.execute(
                    "INSERT INTO evidence(source_id,locator_type,locator_start,locator_end,content,content_hash) VALUES(?,?,?,?,?,?)",
                    (source_id, 'line', 2, 2, '乙', 'b'),
                ).lastrowid
                for name in ('甲节点', '乙节点'):
                    candidate_id = conn.execute(
                        "INSERT INTO knowledge_candidates(source_id,schema_version,name,chapter) VALUES(?,?,?,?)",
                        (source_id, 'cyberpal.knowledge.v1', name, '第1章'),
                    ).lastrowid
                    evidence_id = evidence_a if name.startswith('甲') else evidence_b
                    conn.execute(
                        "INSERT INTO knowledge_candidate_evidence(candidate_id,evidence_id) VALUES(?,?)",
                        (candidate_id, evidence_id),
                    )
                conn.commit()
                ids = [row[0] for row in conn.execute(
                    "SELECT id FROM knowledge_candidates ORDER BY id"
                ).fetchall()]
                conn.close()

                sources.confirm_candidate(ids[0])
                sources.confirm_candidate(ids[1])

                conn = db.connect()
                try:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM relation_candidates").fetchone()[0], 0)
                finally:
                    conn.close()

    def test_confirming_nodes_with_shared_evidence_does_not_pair_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'):
                db.apply_schema()
                conn = db.connect()
                conn.execute("INSERT INTO courses(name) VALUES('测试课程')")
                course_id = conn.execute("SELECT id FROM courses").fetchone()[0]
                source_id = conn.execute(
                    "INSERT INTO source_files(course_id,path,status) VALUES(?,?,?)",
                    (course_id, '/tmp/book.md', 'indexed'),
                ).lastrowid
                evidence_id = conn.execute(
                    "INSERT INTO evidence(source_id,locator_type,locator_start,locator_end,content,content_hash) VALUES(?,?,?,?,?,?)",
                    (source_id, 'line', 1, 1, '共同证据', 'shared'),
                ).lastrowid
                candidate_ids = []
                for name in ('甲节点', '乙节点'):
                    candidate_id = conn.execute(
                        "INSERT INTO knowledge_candidates(source_id,schema_version,name,chapter) VALUES(?,?,?,?)",
                        (source_id, 'cyberpal.knowledge.v1', name, '第1章'),
                    ).lastrowid
                    conn.execute(
                        "INSERT INTO knowledge_candidate_evidence(candidate_id,evidence_id) VALUES(?,?)",
                        (candidate_id, evidence_id),
                    )
                    candidate_ids.append(candidate_id)
                conn.commit()
                conn.close()

                sources.confirm_candidate(candidate_ids[0])
                sources.confirm_candidate(candidate_ids[1])

                conn = db.connect()
                try:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM relation_candidates").fetchone()[0], 0)
                finally:
                    conn.close()


if __name__ == '__main__':
    unittest.main()
