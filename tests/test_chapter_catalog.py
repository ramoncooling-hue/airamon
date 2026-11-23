import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.memory import chapter_catalog, db, repo


class ChapterCatalogTests(unittest.TestCase):
    def test_extract_catalog_keeps_canonical_chapter_order_and_sections(self):
        evidence = [
            {'locator_type': 'page', 'locator_start': 1,
             'text': '第 2 章 一个完整的业务循环 学习目的\n2.1 资金循环\n2.2 筹资活动的核算'},
            {'locator_type': 'page', 'locator_start': 2,
             'text': '第 1 章 财务会计的方法与原则 学习目的\n1.1 企业经济活动与会计核算'},
        ]

        chapters = chapter_catalog.extract_catalog(evidence)

        self.assertEqual([item['number'] for item in chapters], [1, 2])
        self.assertEqual(chapters[0]['key'], 'ch-01')
        self.assertEqual(chapters[0]['sections'][0]['key'], 'ch-01-sec-01')
        self.assertEqual(chapters[0]['sections'][0]['title'], '企业经济活动与会计核算')

    def test_extract_catalog_preserves_nested_section_keys(self):
        evidence = [{'locator_type': 'page', 'locator_start': 1,
                     'text': '第 3 章 财务报表 学习目的\n'
                             '3.1 资产负债表\n'
                             '3 . 1. 1 资产、负债和所有者权益及其分类\n'
                             '3.1.2 资产负债表的格式\n'
                             '3.2 利润表'}]

        chapters = chapter_catalog.extract_catalog(evidence)

        self.assertEqual(
            [(item['number'], item['key']) for item in chapters[0]['sections']],
            [('3.1', 'ch-03-sec-01'), ('3.1.1', 'ch-03-sec-01-01'),
             ('3.1.2', 'ch-03-sec-01-02'), ('3.2', 'ch-03-sec-02')],
        )

    def test_graph_returns_catalog_and_stable_node_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'):
                db.apply_schema()
                conn = db.connect()
                conn.execute("INSERT INTO courses(name) VALUES('测试课程')")
                course_id = conn.execute("SELECT id FROM courses WHERE name='测试课程'").fetchone()[0]
                conn.execute('''INSERT INTO chapter_catalog
                    (course_id,chapter_key,number,title,status,source_sha256)
                    VALUES(?,?,?,?,?,?)''', (course_id, 'ch-01', 1, '第一章', 'active', 'sha'))
                chapter_id = conn.execute('SELECT id FROM chapter_catalog').fetchone()[0]
                conn.execute('''INSERT INTO chapter_sections
                    (chapter_id,section_key,number,title,status)
                    VALUES(?,?,?,?,?)''', (chapter_id, 'ch-01-sec-01', '1.1', '第一节', 'active'))
                conn.execute('''INSERT INTO kp(course_id,name,chapter,chapter_key,section_key)
                    VALUES(?,?,?,?,?)''', (course_id, '节点A', '第一章', 'ch-01', 'ch-01-sec-01'))
                conn.commit()
                conn.close()

                graph = repo.graph_of('测试课程')

                self.assertEqual([item['key'] for item in graph['chapters']], ['ch-01'])
                self.assertEqual(graph['catalog_status'], 'ready')
                self.assertEqual(graph['chapters'][0]['node_ids'], [1])
                self.assertEqual(graph['nodes'][0]['chapter_key'], 'ch-01')
                self.assertEqual(graph['nodes'][0]['section_key'], 'ch-01-sec-01')

    def test_graph_backfills_nested_section_from_evidence_locator(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'):
                db.apply_schema()
                conn = db.connect()
                conn.execute("INSERT INTO courses(name) VALUES('测试课程')")
                course_id = conn.execute('SELECT id FROM courses').fetchone()[0]
                source_id = conn.execute(
                    "INSERT INTO source_files(course_id,path,sha256,status) VALUES(?,?,?,?)",
                    (course_id, '/tmp/book.pdf', 'sha', 'indexed'),
                ).lastrowid
                chapter_id = conn.execute(
                    "INSERT INTO chapter_catalog(course_id,source_id,chapter_key,number,title,start_locator,end_locator,source_sha256,catalog_version) VALUES(?,?,?,?,?,?,?,?,?)",
                    (course_id, source_id, 'ch-03', 3, '财务报表', 1, 10, 'sha', '2'),
                ).lastrowid
                conn.execute(
                    "INSERT INTO chapter_sections(chapter_id,section_key,number,title,start_locator,end_locator) VALUES(?,?,?,?,?,?)",
                    (chapter_id, 'ch-03-sec-01-01', '3.1.1', '分类', 2, 4),
                )
                evidence_id = conn.execute(
                    "INSERT INTO evidence(source_id,locator_type,locator_start,locator_end,content,content_hash) VALUES(?,?,?,?,?,?)",
                    (source_id, 'page', 3, 3, '历史成本计量', 'history'),
                ).lastrowid
                conn.execute(
                    "INSERT INTO kp(course_id,name,chapter,chapter_key,section_key) VALUES(?,?,?,?,?)",
                    (course_id, '历史成本计量', '第3章', 'ch-03', ''),
                )
                kp_id = conn.execute('SELECT id FROM kp').fetchone()[0]
                conn.execute("INSERT INTO kp_evidence(kp_id,evidence_id) VALUES(?,?)", (kp_id, evidence_id))
                conn.commit()
                conn.close()

                graph = repo.graph_of('测试课程')

                self.assertEqual(graph['nodes'][0]['section_key'], 'ch-03-sec-01-01')

    def test_section_anchor_nodes_create_parent_relation_for_section_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'):
                db.apply_schema()
                conn = db.connect()
                conn.execute("INSERT INTO courses(name) VALUES('测试课程')")
                course_id = conn.execute('SELECT id FROM courses').fetchone()[0]
                source_id = conn.execute(
                    "INSERT INTO source_files(course_id,path,sha256,status) VALUES(?,?,?,?)",
                    (course_id, '/tmp/book.pdf', 'sha', 'indexed'),
                ).lastrowid
                chapter_id = conn.execute(
                    "INSERT INTO chapter_catalog(course_id,source_id,chapter_key,number,title,start_locator,end_locator,source_sha256,catalog_version) VALUES(?,?,?,?,?,?,?,?,?)",
                    (course_id, source_id, 'ch-03', 3, '财务报表', 1, 10, 'sha', '2'),
                ).lastrowid
                conn.execute(
                    "INSERT INTO chapter_sections(chapter_id,section_key,number,title,start_locator,end_locator) VALUES(?,?,?,?,?,?)",
                    (chapter_id, 'ch-03-sec-01-01', '3.1.1', '资产分类', 2, 4),
                )
                evidence_id = conn.execute(
                    "INSERT INTO evidence(source_id,locator_type,locator_start,locator_end,content,content_hash) VALUES(?,?,?,?,?,?)",
                    (source_id, 'page', 3, 3, '资产分类', 'asset'),
                ).lastrowid
                conn.execute(
                    "INSERT INTO kp(course_id,name,chapter,chapter_key,section_key) VALUES(?,?,?,?,?)",
                    (course_id, '资产知识点', '第3章', 'ch-03', 'ch-03-sec-01-01'),
                )
                conn.commit()
                conn.close()

                result = repo.backfill_section_nodes('测试课程')
                graph = repo.graph_of('测试课程')

                self.assertEqual(result['created'], 1)
                self.assertEqual(result['relations'], 1)
                self.assertTrue(any(node['name'] == '资产分类' for node in graph['nodes']))
                self.assertEqual(
                    next(node for node in graph['nodes'] if node['name'] == '资产分类')['section_key'],
                    'ch-03-sec-01-01',
                )
                self.assertEqual(graph['edges'][0]['kind'], 'part_of')

                second = repo.backfill_section_nodes('测试课程')
                self.assertEqual(second, {'created': 0, 'relations': 0})
                conn = db.connect()
                try:
                    self.assertEqual(
                        conn.execute("SELECT COUNT(*) FROM kp WHERE kp_type='section'").fetchone()[0],
                        1,
                    )
                finally:
                    conn.close()

    def test_structural_text_is_not_a_formal_graph_node(self):
        self.assertTrue(chapter_catalog.is_structural_node(
            {'name': '第12章', 'kp_type': 'material', 'chapter': '来源材料'}
        ))
        self.assertTrue(chapter_catalog.is_structural_node(
            {'name': '第3章将结合资产计价方法详细介绍历史成本和公允价值计量。',
             'kp_type': 'material', 'chapter': '来源材料'}
        ))
        self.assertFalse(chapter_catalog.is_structural_node(
            {'name': '资产负债表', 'kp_type': 'material', 'chapter': '第3章'}
        ))

    def test_catalog_does_not_fall_back_to_an_unclassified_textbook(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'):
                db.apply_schema()
                conn = db.connect()
                course_id = conn.execute("INSERT INTO courses(name) VALUES('测试课程')").lastrowid
                conn.execute(
                    "INSERT INTO source_files(course_id,path,sha256,status) VALUES(?,?,?,?)",
                    (course_id, '/tmp/textbook.pdf', 'book', 'indexed'),
                )
                conn.commit()
                conn.close()
                self.assertEqual(repo.ensure_chapter_catalog('测试课程'), [])
                self.assertEqual(repo.graph_of('测试课程')['catalog_status'], 'missing')

    def test_select_catalog_source_marks_only_selected_upload_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'):
                db.apply_schema()
                conn = db.connect()
                course_id = conn.execute("INSERT INTO courses(name) VALUES('测试课程')").lastrowid
                first = conn.execute(
                    "INSERT INTO source_files(course_id,path,sha256,status,source_kind,catalog_status) VALUES(?,?,?,?,?,?)",
                    (course_id, '/tmp/catalog-a.md', 'a', 'indexed', 'course_catalog', 'candidate'),
                ).lastrowid
                second = conn.execute(
                    "INSERT INTO source_files(course_id,path,sha256,status,source_kind,catalog_status) VALUES(?,?,?,?,?,?)",
                    (course_id, '/tmp/catalog-b.md', 'b', 'indexed', 'course_catalog', 'candidate'),
                ).lastrowid
                conn.commit()
                conn.close()
                with patch('app.memory.sources.extract_structured', return_value=('', [], [
                    {'locator_type': 'page', 'locator_start': 1, 'text': '第1章 需求 学习目的\n1.1 需求曲线'},
                ])):
                    result = repo.select_catalog_source('测试课程', second)
                self.assertEqual(result['source_id'], second)
                conn = db.connect()
                try:
                    rows = conn.execute('SELECT id,catalog_status FROM source_files ORDER BY id').fetchall()
                    self.assertEqual([(row['id'], row['catalog_status']) for row in rows], [(first, 'historical'), (second, 'active')])
                finally:
                    conn.close()


if __name__ == '__main__':
    unittest.main()
