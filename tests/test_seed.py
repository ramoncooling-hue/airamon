import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.memory import db
from scripts import load_memory


class SeedRefreshTests(unittest.TestCase):
    def test_loads_workbench_schedule_and_notice_seed_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "seed"
            source.mkdir()
            (source / "dash_courses.json").write_text(json.dumps([
                {"课程": "组织行为学", "教师": "郑晓明", "学分": 2},
                {"课程": "管理沟通", "教师": "刘茜", "学分": 1},
            ], ensure_ascii=False), encoding="utf-8")
            (source / "dash_sched.json").write_text(json.dumps([
                {"record_id": "wb-event-1", "课程": "管理沟通", "标题": "管理沟通（调课）",
                 "类型": "课程", "日期": "2026-10-03", "时间": "09:00–12:00",
                 "地点": "舜德楼302", "备注": "来自工作台课程安排", "来源": "workbench"},
                {"record_id": "wb-event-2", "课程": "组织行为学", "标题": "提交课前预习报告",
                 "类型": "作业", "日期": "2026-09-28", "时间": "08:00前",
                 "地点": "", "备注": "来自工作台课程群消息", "来源": "workbench"},
            ], ensure_ascii=False), encoding="utf-8")
            (source / "dash_notices.json").write_text(json.dumps([
                {"record_id": "wb-notice-1", "课程": "组织行为学", "标题": "课程群通知：下周一交预习报告",
                 "正文": "下周一交预习报告", "日期": "2026-09-27", "来源": "workbench/inbox"},
            ], ensure_ascii=False), encoding="utf-8")
            for name in ("组织行为学", "管理经济学", "会计学"):
                (source / f"recs_{name}_kp.json").write_text("[]", encoding="utf-8")
            with patch.object(db, "APP_DIR", root / "app"), patch.object(db, "DB_PATH", root / "app" / "test.db"), patch.object(load_memory, "DATA", source):
                load_memory.main([])
                load_memory.main([])
                conn = db.connect()
                try:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 2)
                    self.assertEqual(conn.execute("SELECT title FROM events WHERE record_id='wb-event-2'").fetchone()[0], "提交课前预习报告")
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM notices").fetchone()[0], 1)
                    self.assertEqual(conn.execute("SELECT body FROM notices WHERE record_id='wb-notice-1'").fetchone()[0], "下周一交预习报告")
                finally:
                    conn.close()

    def test_reimport_preserves_personal_mastery_and_skips_duplicate_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "seed"
            source.mkdir()
            (source / "dash_courses.json").write_text(json.dumps([
                {"课程": "会计学", "教师": "老师", "学分": 2}
            ], ensure_ascii=False), encoding="utf-8")
            (source / "recs_会计学_kp.json").write_text(json.dumps([
                {"record_id": "kp-1", "知识点": "资产负债表", "一句话": "课程基础", "掌握度": "未读"}
            ], ensure_ascii=False), encoding="utf-8")
            for name in ("组织行为学", "管理经济学"):
                (source / f"recs_{name}_kp.json").write_text("[]", encoding="utf-8")
            with patch.object(db, "APP_DIR", root / "app"), patch.object(db, "DB_PATH", root / "app" / "test.db"), patch.object(load_memory, "DATA", source):
                load_memory.main([])
                conn = db.connect()
                conn.execute("UPDATE kp SET mastery='熟练' WHERE name='资产负债表'")
                conn.execute("INSERT INTO courses(name) VALUES('个人选修课')")
                conn.commit()
                conn.close()
                (source / "recs_会计学_kp.json").write_text(json.dumps([
                    {"record_id": "kp-1", "知识点": "资产负债表", "一句话": "课程基础", "掌握度": "未读"},
                    {"record_id": "kp-2", "知识点": "新增知识点", "一句话": "新增内容", "掌握度": "未读"}
                ], ensure_ascii=False), encoding="utf-8")
                load_memory.main([])
                conn = db.connect()
                try:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM kp").fetchone()[0], 2)
                    self.assertEqual(conn.execute("SELECT mastery FROM kp WHERE name='资产负债表'").fetchone()[0], "熟练")
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM courses WHERE name='个人选修课'").fetchone()[0], 1)
                finally:
                    conn.close()


if __name__ == "__main__":
    unittest.main()
