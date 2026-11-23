import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.memory import db, relation_suggestions


class RelationSuggestionTests(unittest.TestCase):
    def test_node_mapping_requires_a_full_name_or_alias_match(self):
        self.assertEqual(relation_suggestions._names_in('资产负债表', [('资产', 1)]), [])
        self.assertEqual(relation_suggestions._names_in('资产', [('资产', 1)]), [1])

    def test_extracts_only_evidenced_wiki_links_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            notes = root / 'notes'
            dictionaries = root / 'dictionary'
            notes.mkdir()
            dictionaries.mkdir()
            note_path = notes / 'book.md'
            note_path.write_text('draft', encoding='utf-8')
            dictionary_path = dictionaries / 'book.json'
            dictionary_path.write_text(json.dumps({
                'schema': 'cyberpal.dictionary.v2',
                'model_assisted': True,
                'links': ['资产 -> 负债（证据1）', '收入 -> 虚构节点（证据2）'],
                'evidence': [
                    {'locator_type': 'page', 'locator_start': 10, 'locator_end': 10,
                     'text': '资产与负债共同构成资产负债表。'},
                    {'locator_type': 'page', 'locator_start': 11, 'locator_end': 11,
                     'text': '收入应按规则确认。'},
                ],
            }, ensure_ascii=False), encoding='utf-8')

            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'):
                db.apply_schema()
                conn = db.connect()
                course_id = conn.execute("INSERT INTO courses(name) VALUES('会计学')").lastrowid
                source_id = conn.execute(
                    "INSERT INTO source_files(course_id,path,status) VALUES(?,?,?)",
                    (course_id, '/tmp/book.pdf', 'indexed'),
                ).lastrowid
                first_evidence = conn.execute(
                    "INSERT INTO evidence(source_id,locator_type,locator_start,locator_end,content,content_hash) VALUES(?,?,?,?,?,?)",
                    (source_id, 'page', 10, 10, '资产与负债共同构成资产负债表。', 'e1'),
                ).lastrowid
                conn.execute(
                    "INSERT INTO evidence(source_id,locator_type,locator_start,locator_end,content,content_hash) VALUES(?,?,?,?,?,?)",
                    (source_id, 'page', 11, 11, '收入应按规则确认。', 'e2'),
                )
                a = conn.execute("INSERT INTO kp(course_id,name,chapter) VALUES(?,?,?)", (course_id, '资产', '第2章')).lastrowid
                b = conn.execute("INSERT INTO kp(course_id,name,chapter) VALUES(?,?,?)", (course_id, '负债', '第10章')).lastrowid
                conn.execute("INSERT INTO knowledge_notes(source_id,path,source_sha256,status) VALUES(?,?,?,?)",
                             (source_id, str(note_path), 'sha', 'assisted_draft'))
                conn.commit()
                conn.close()

                first = relation_suggestions.generate('会计学')
                second = relation_suggestions.generate('会计学')

                self.assertEqual(first['created'], 1)
                self.assertEqual(first['unmapped'], 1)
                self.assertEqual(second['created'], 0)
                self.assertEqual(second['duplicates'], 1)
                rows = db.connect().execute("SELECT * FROM relation_candidates WHERE status='pending'").fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual((rows[0]['from_kp'], rows[0]['to_kp']), (a, b))
                self.assertEqual(rows[0]['evidence_id'], first_evidence)

    def test_does_not_create_candidate_without_source_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            notes = root / 'notes'
            dictionaries = root / 'dictionary'
            notes.mkdir()
            dictionaries.mkdir()
            note_path = notes / 'book.md'
            note_path.write_text('draft', encoding='utf-8')
            (dictionaries / 'book.json').write_text(json.dumps({
                'schema': 'cyberpal.dictionary.v2', 'model_assisted': True,
                'links': ['资产 -> 负债'], 'evidence': [],
            }, ensure_ascii=False), encoding='utf-8')

            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'):
                db.apply_schema()
                conn = db.connect()
                course_id = conn.execute("INSERT INTO courses(name) VALUES('会计学')").lastrowid
                source_id = conn.execute(
                    "INSERT INTO source_files(course_id,path,status) VALUES(?,?,?)",
                    (course_id, '/tmp/book.pdf', 'indexed'),
                ).lastrowid
                conn.execute("INSERT INTO kp(course_id,name) VALUES(?,?)", (course_id, '资产'))
                conn.execute("INSERT INTO kp(course_id,name) VALUES(?,?)", (course_id, '负债'))
                conn.execute("INSERT INTO knowledge_notes(source_id,path,source_sha256,status) VALUES(?,?,?,?)",
                             (source_id, str(note_path), 'sha', 'assisted_draft'))
                conn.commit()
                conn.close()

                result = relation_suggestions.generate('会计学')
                self.assertEqual(result['created'], 0)
                self.assertEqual(result['no_evidence'], 1)
                conn = db.connect()
                try:
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM relation_candidates').fetchone()[0], 0)
                finally:
                    conn.close()

    def test_ai_suggestions_are_review_only_and_must_map_to_nodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'), \
                 patch.object(relation_suggestions.llm, 'chat', return_value=json.dumps({
                     'relations': [
                         {'from_name': '概念A', 'to_name': '概念B', 'kind': 'pre', 'reason': 'A 是 B 的基础', 'evidence_id': 1},
                         {'from_name': '不存在', 'to_name': '概念B', 'kind': 'related', 'reason': '不应入队'},
                     ]
                 }, ensure_ascii=False)):
                db.apply_schema()
                conn = db.connect()
                cid = conn.execute("INSERT INTO courses(name) VALUES('测试课程')").lastrowid
                sid = conn.execute(
                    "INSERT INTO source_files(course_id,path,status) VALUES(?,?,?)",
                    (cid, '/tmp/book.pdf', 'indexed'),
                ).lastrowid
                evidence_id = conn.execute(
                    "INSERT INTO evidence(source_id,locator_type,locator_start,locator_end,content,content_hash) VALUES(?,?,?,?,?,?)",
                    (sid, 'page', 2, 2, '概念A 是概念B 的基础。', 'e1'),
                ).lastrowid
                a = conn.execute("INSERT INTO kp(course_id,name,chapter,gist) VALUES(?,?,?,?)",
                                 (cid, '概念A', '第1章', 'A')).lastrowid
                b = conn.execute("INSERT INTO kp(course_id,name,chapter,gist) VALUES(?,?,?,?)",
                                 (cid, '概念B', '第2章', 'B')).lastrowid
                conn.execute("INSERT INTO kp_evidence(kp_id,evidence_id) VALUES(?,?)", (a, evidence_id))
                conn.execute("INSERT INTO kp_evidence(kp_id,evidence_id) VALUES(?,?)", (b, evidence_id))
                conn.commit()
                conn.close()

                result = relation_suggestions.generate_ai('测试课程')
                self.assertEqual(result['created'], 1)
                self.assertEqual(result['unmapped'], 1)
                row = db.connect().execute("SELECT * FROM relation_candidates").fetchone()
                self.assertEqual((row['from_kp'], row['to_kp'], row['status']), (a, b, 'pending'))
                self.assertEqual(row['evidence_id'], evidence_id)


if __name__ == '__main__':
    unittest.main()
