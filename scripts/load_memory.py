#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 seed/ 的公开初始 JSON 灌进记忆库。

按「记忆的生命周期」归类，不按键名硬对：
    课程   → courses      （记忆的容器）
    知识点 → kp           （记忆单元，带溯源）
    前置   → kp_edges(pre)
    桥接   → bridges      （跨课关系）
    进度   → progress     （记忆的推进）
    题库   → quiz         （记忆的自测）
    日程   → events       （记忆的时间投影，按类型补 category）

用法：
    python3 scripts/load_memory.py            # 灌数
    python3 scripts/load_memory.py --check    # 只核对，不写入
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
import argparse
from contextlib import nullcontext

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / "seed"
sys.path.insert(0, str(ROOT))

from app.memory.db import apply_schema, connect  # noqa: E402

COURSES = ["会计学", "组织行为学", "管理经济学"]

# 语义带：图谱分带用。会计学沿用已定稿的三带口径；另两课按讲次结构分组。
BAND_ACCOUNTING_2 = {
    "借贷记账法", "试算平衡与错误修正", "会计核算程序", "逐笔业务判断方法论",
    "资产负债表项目关系式", "五种计量属性", "历史成本的两个前提",
    "期末账项调整与利润形成", "权责发生制与收付实现制", "资金循环与三大活动核算",
}
BAND_ACCOUNTING_3 = {
    "资产负债表：资源与资源来源", "资产的三特征与两确认条件", "负债的三特征与两确认条件",
    "所有者权益四大项目", "资产负债表平衡式", "流动与非流动的划分标准",
    "表外资源与表外义务", "三个短期偿债能力指标", "三个长期偿债能力指标",
    "报表勾稽关系", "时点报表与时期报表",
}


def band_of_accounting(name: str) -> str:
    if name in BAND_ACCOUNTING_2:
        return "02"
    if name in BAND_ACCOUNTING_3:
        return "03"
    return "01"


def collect_chapter_aliases() -> list[str]:
    """把源数据里所有「完整」章节写法收集起来，供 normalize_chapter 补全用。
    只收带章名的（「第1章 导论」），不收只有编号的（「第1章」）。"""
    names: list[str] = []
    for course in COURSES:
        p = DATA / f"recs_{course}_kp.json"
        if not p.exists():
            continue
        for r in json.loads(p.read_text(encoding="utf-8")):
            ch = (r.get("章节") or "").strip()
            if "/" in ch:
                continue
            if re.fullmatch(r"第\d+[章讲][^\s/]*\s+.+", ch) or re.fullmatch(r"[^第].+", ch):
                if ch and ch not in names:
                    names.append(ch)
    return names


CHAPTER_ALIAS: list[str] = []


def normalize_chapter(chapter: str) -> str:
    """原库里有跨章条目写成「第1章 / 第2章」，矩阵里会多出一行。
    归到它主要讲的那一章。同时把「第1章」这种只有编号的写法，
    在源数据里存在「第1章 导论」时补成完整名，避免同一章裂成两行。"""
    chapter = (chapter or "").strip()
    if "/" in chapter:
        first = re.split(r"\s*/\s*", chapter)[0].strip()
        if first:
            chapter = first
    # 只有章号没有章名的，去源数据里找完整写法
    m = re.fullmatch(r"(第\d+章)", chapter)
    if m and CHAPTER_ALIAS:
        full = next((c for c in CHAPTER_ALIAS if c.startswith(m.group(1) + " ")), None)
        if full:
            return full
    return chapter


def band_of_ob(chapter: str) -> str:
    """组织行为学：个体—群体—组织三层，这正是它课程自己的骨架。"""
    m = re.search(r"第(\d)讲", chapter)
    n = int(m.group(1)) if m else 0
    if n <= 2:
        return "01"   # 个体层
    if n <= 5:
        return "02"   # 群体层
    return "03"       # 组织层


def band_of_me(chapter: str) -> str:
    """管理经济学：框架 → 需求与估计 → 生产与成本 → 市场结构。"""
    if "参考框架" in chapter or "第一讲" in chapter:
        return "01"
    if "第二讲" in chapter:
        return "02"
    if "第三讲" in chapter:
        return "03"
    return "04"


def band_of(course: str, name: str, chapter: str) -> str:
    if course == "会计学":
        return band_of_accounting(name)
    if course == "组织行为学":
        return band_of_ob(chapter)
    return band_of_me(chapter)


def split_list(text: str) -> list[str]:
    if not text:
        return []
    return [p.strip() for p in re.split(r"[、;；,，/]", text) if p.strip()]


def event_category(type_raw: str, title: str) -> str:
    """把原库的「类型」归到四色日程口径。"""
    t = (type_raw or "") + (title or "")
    if "考试" in t or "期末" in t:
        return "exam"
    if "作业" in t or "DDL" in t or "提交" in t:
        return "hw"
    if "上课" in t or "讲" in t or "面授" in t:
        return "class"
    return "personal"


def load_courses(conn) -> dict[str, int]:
    """课程档案 + 两门待补课（数据侧无档案，标注而不是伪造）。"""
    rows = json.loads((DATA / "dash_courses.json").read_text(encoding="utf-8"))
    id_by_name: dict[str, int] = {}
    for i, r in enumerate(rows):
        name = (r.get("课程") or "").strip()
        if not name:
            continue
        conn.execute(
            """INSERT OR IGNORE INTO courses (name, teacher, classroom, schedule, nature, credits,
                                    assessment, note, status, sort_order)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (name, r.get("教师", ""), r.get("教室", ""), r.get("上课时间", ""),
             r.get("性质", ""), float(r.get("学分") or 0), r.get("考核方式", ""),
             r.get("备注", ""), "在读", i),
        )
        id_by_name[name] = conn.execute("SELECT id FROM courses WHERE name=?", (name,)).fetchone()[0]

    # seed 里出现但没有课程档案的，标「待补」，不编造详情。
    # 用包含匹配判重：档案里写的是「数据和模型与决策（DMD）」，
    # 直接比全名会漏判、多出一门重复课。
    for name in ("数据和模型与决策", "马克思主义与当代科技"):
        if any(name in existing for existing in id_by_name):
            continue
        conn.execute(
            "INSERT OR IGNORE INTO courses (name, status, sort_order) VALUES (?,?,?)",
            (name, "待补", len(id_by_name) + 1),
        )
        id_by_name[name] = conn.execute("SELECT id FROM courses WHERE name=?", (name,)).fetchone()[0]
    return id_by_name


def load_kp(conn, course_ids: dict[str, int]) -> int:
    total = 0
    for course in COURSES:
        cid = course_ids.get(course)
        if not cid:
            continue
        path = DATA / f"recs_{course}_kp.json"
        if not path.exists():
            continue
        rows = json.loads(path.read_text(encoding="utf-8"))
        for r in rows:
            name = (r.get("知识点") or "").strip()
            if not name:
                continue
            chapter = normalize_chapter(r.get("章节", ""))
            conn.execute(
                """INSERT OR IGNORE INTO kp
                   (course_id, record_id, name, band, chapter, gist, detail, trap,
                    source, related, kp_type, freq, mastery)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (cid, r.get("record_id", ""), name,
                 band_of(course, name, chapter), chapter,
                 r.get("一句话", ""), r.get("详解", ""), r.get("易错点", ""),
                 r.get("溯源", ""), r.get("关联", ""), r.get("类型", ""),
                 r.get("考频", ""), r.get("掌握度", "未读")),
            )
            total += 1
    return total


def load_edges(conn) -> tuple[int, int]:
    """关系边：原库字段里的「关联」→ related 边；「前置」→ pre 边（若有）。"""
    pre = rel = 0
    rows = conn.execute("SELECT id, course_id, name, related FROM kp").fetchall()
    idx = {(r["course_id"], r["name"]): r["id"] for r in rows}
    for r in rows:
        seen: set[int] = set()
        for other in split_list(r["related"] or ""):
            kid = idx.get((r["course_id"], other))
            if kid and kid != r["id"] and kid not in seen:
                seen.add(kid)
                conn.execute(
                    "INSERT OR IGNORE INTO kp_edges (from_kp, to_kp, kind) VALUES (?,?,'related')",
                    (r["id"], kid),
                )
                rel += 1

    # 前置边：真源在原型图谱脚本的补白名单里（原库字段带「前置」的条目很少）
    for course in COURSES:
        path = DATA / f"recs_{course}_kp.json"
        if not path.exists():
            continue
        kp_json = json.loads(path.read_text(encoding="utf-8"))
        for item in kp_json:
            target = idx.get((None, item["知识点"]))
            for p in split_list(item.get("前置") or ""):
                for (cid, nm), kid in idx.items():
                    if nm == p:
                        tgt = idx.get((cid, item["知识点"]))
                        if tgt and kid != tgt:
                            conn.execute(
                                "INSERT OR IGNORE INTO kp_edges (from_kp, to_kp, kind) VALUES (?,?,'pre')",
                                (kid, tgt),
                            )
                            pre += 1
                        break
            del target
    return pre, rel


def load_bridges(conn) -> int:
    path = DATA / "dash_bridge.json"
    if not path.exists():
        return 0
    rows = json.loads(path.read_text(encoding="utf-8"))
    n = 0
    for r in rows:
        purpose = r.get("用途")
        if conn.execute("""SELECT 1 FROM bridges WHERE course_a=? AND kp_a=? AND course_b=?
                           AND kp_b=? AND topic=?""",
                        (r.get("课程A", ""), r.get("知识点A", ""), r.get("课程B", ""),
                         r.get("知识点B", ""), r.get("桥接主题", ""))).fetchone():
            continue
        conn.execute(
            """INSERT INTO bridges (course_a, kp_a, course_b, kp_b, topic, logic, purpose, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (r.get("课程A", ""), r.get("知识点A", ""), r.get("课程B", ""), r.get("知识点B", ""),
             r.get("桥接主题", ""), r.get("桥接逻辑", ""),
             "、".join(purpose) if isinstance(purpose, list) else (purpose or ""),
             r.get("更新日期", "")),
        )
        n += 1
    return n


def load_progress(conn, course_ids: dict[str, int]) -> int:
    n = 0
    for course in COURSES:
        cid = course_ids.get(course)
        path = DATA / f"recs_{course}_prog.json"
        if not cid or not path.exists():
            continue
        for r in json.loads(path.read_text(encoding="utf-8")):
            if conn.execute("""SELECT 1 FROM progress WHERE course_id=? AND
                           ((record_id <> '' AND record_id=?) OR (seq=? AND date=? AND homework=?))""",
                            (cid, r.get("record_id", ""), r.get("序号", ""),
                             r.get("日期", ""), r.get("作业", ""))).fetchone():
                continue
            conn.execute(
                """INSERT INTO progress (course_id, record_id, seq, topic, homework, note,
                                         due_at, submit_via, status, date, prereq)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (cid, r.get("record_id", ""), r.get("序号", ""), r.get("主题", ""),
                 r.get("作业", ""), r.get("备注", ""), r.get("截止", "") or r.get("截止时间", ""),
                 r.get("提交方式", ""), r.get("状态", ""), r.get("日期", ""), r.get("预习要求", "")),
            )
            n += 1
    return n


def load_quiz(conn, course_ids: dict[str, int]) -> int:
    n = 0
    for course in COURSES:
        cid = course_ids.get(course)
        path = DATA / f"recs_{course}_qb.json"
        if not cid or not path.exists():
            continue
        for r in json.loads(path.read_text(encoding="utf-8")):
            if conn.execute("""SELECT 1 FROM quiz WHERE course_id=? AND
                           ((record_id <> '' AND record_id=?) OR (kp_name=? AND stem=?))""",
                            (cid, r.get("record_id", ""), r.get("知识点", ""),
                             r.get("题干", ""))).fetchone():
                continue
            kp_name = r.get("知识点", "")
            kp_row = conn.execute(
                "SELECT id FROM kp WHERE course_id=? AND name=?", (cid, kp_name)
            ).fetchone()
            conn.execute(
                """INSERT INTO quiz (course_id, kp_id, record_id, kp_name, source, stem, answer, analysis, difficulty)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (cid, kp_row["id"] if kp_row else None, r.get("record_id", ""), kp_name,
                 r.get("来源", ""), r.get("题干", ""), r.get("答案", ""),
                 r.get("解析", ""), r.get("难度", "")),
            )
            n += 1
    return n


def load_events(conn, course_ids: dict[str, int]) -> int:
    path = DATA / "dash_sched.json"
    if not path.exists():
        return 0
    rows = json.loads(path.read_text(encoding="utf-8"))
    n = 0
    for r in rows:
        cname = (r.get("课程") or "").strip()
        # 原库里「课程」多为空，从标题里认课名
        if not cname:
            for c in course_ids:
                if c and c in (r.get("标题") or ""):
                    cname = c
                    break
        cid = course_ids.get(cname)
        ttype = r.get("类型", "")
        if conn.execute("""SELECT 1 FROM events WHERE
                        (record_id <> '' AND record_id=?) OR (date=? AND title=? AND course_name=?)""",
                        (r.get("record_id", ""), r.get("日期", ""), r.get("标题", ""), cname)).fetchone():
            continue
        conn.execute(
            """INSERT INTO events (record_id, course_id, course_name, title, category,
                                   type_raw, date, time_range, location, note, source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (r.get("record_id", ""), cid, cname, r.get("标题", ""),
             event_category(ttype, r.get("标题", "")), ttype,
             r.get("日期", ""), r.get("时间", ""), r.get("地点", ""), r.get("备注", ""),
             r.get("来源", "seed")),
        )
        n += 1
    return n


def load_notices(conn, course_ids: dict[str, int]) -> int:
    """Load source notices separately so chat can cite the original message."""
    path = DATA / "dash_notices.json"
    if not path.exists():
        return 0
    rows = json.loads(path.read_text(encoding="utf-8"))
    n = 0
    for r in rows:
        record_id = str(r.get("record_id") or "").strip()
        if record_id and conn.execute("SELECT 1 FROM notices WHERE record_id=?", (record_id,)).fetchone():
            continue
        cname = str(r.get("课程") or "").strip()
        title = str(r.get("标题") or "课程通知").strip()
        if not title:
            continue
        conn.execute(
            """INSERT INTO notices (record_id, course_id, course_name, title, body, date, source, url)
               VALUES (?,?,?,?,?,?,?,?)""",
            (record_id, course_ids.get(cname), cname, title, str(r.get("正文") or "").strip(),
             str(r.get("日期") or "").strip(), str(r.get("来源") or "seed").strip(),
             str(r.get("链接") or "").strip()),
        )
        n += 1
    return n


def main(argv: list[str] | None = None) -> None:
    from app.memory import db
    from app.maintenance.lock import check_legacy_desktop, owned_by_process, runtime_lock
    root = db.DB_PATH.parent.parent
    # Native/source refresh already belongs to the backend. A standalone
    # importer must retain the same exclusion lock for its entire write phase.
    if owned_by_process(root):
        _main(argv)
    else:
        with check_legacy_desktop(root), runtime_lock(root):
            _main(argv)


def _main(argv: list[str] | None = None) -> None:
    global CHAPTER_ALIAS, DATA
    parser = argparse.ArgumentParser(description="Refresh the portable StudyPal starter library")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--data-dir", type=pathlib.Path)
    args = parser.parse_args(argv)
    if args.data_dir:
        DATA = args.data_dir.resolve()
    check_only = args.check
    if not DATA.joinpath("dash_courses.json").exists():
        raise SystemExit(f"初始资料目录缺少 dash_courses.json：{DATA}")
    applied = apply_schema()
    print("已应用 schema:", ", ".join(applied))
    CHAPTER_ALIAS = collect_chapter_aliases()

    conn = connect()
    try:
        if check_only:
            from app.memory.db import table_counts
            print("当前各表行数:", table_counts())
            return

        course_ids = load_courses(conn)
        n_kp = load_kp(conn, course_ids)
        n_pre, n_rel = load_edges(conn)
        n_br = load_bridges(conn)
        n_pg = load_progress(conn, course_ids)
        n_qz = load_quiz(conn, course_ids)
        n_ev = load_events(conn, course_ids)
        n_notice = load_notices(conn, course_ids)
        from app.memory.db import _seed_backfill
        _seed_backfill(conn)
        conn.commit()

        print(f"课程     {len(course_ids)}")
        print(f"知识点   {n_kp}")
        print(f"关系边   {n_pre + n_rel}（前置 {n_pre} / 关联 {n_rel}）")
        print(f"跨课桥接 {n_br}")
        print(f"进度     {n_pg}")
        print(f"题库     {n_qz}")
        print(f"日程     {n_ev}")
        print(f"通知     {n_notice}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
