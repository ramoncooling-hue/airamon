"""复习队列 v0（T3.9）：固定规则 + 三档反馈，不新增表、不做 SRS。

规则（B3-EXECUTION-BRIEF §4，冻结）：
- 未读 = 最高优先，无需等待期；
- 最近一次复习 < 3 天的一律不重复出现（"已读 <7 天不重复"由各档阈值覆盖）；
- "读过"/"会用"且最近复习（无复习记录则取 kp.updated_at）> 3 天；
- "熟练"且 > 14 天；
- 课程考试 ≤7 天时，该课程"高频"知识点优先级 +20（提前复习）。
三档反馈：again→"读过"，hard→"会用"，good 不改档但记事件。
"""
from __future__ import annotations

import json
import logging

from . import db, repo

_LOGGER = logging.getLogger(__name__)

MASTERY_LADDER = ("未读", "读过", "会用", "熟练")
FEEDBACK_RESULT = {"again": "读过", "hard": "会用", "good": None}
MIN_REVIEW_GAP_DAYS = 3
MASTERY_GAP_DAYS = {"读过": 3, "会用": 3, "熟练": 14}
EXAM_SOON_DAYS = 7
EXAM_HIGH_FREQ_BONUS = 20
MAX_LIMIT = 100


class ReviewError(ValueError):
    """复习队列参数/状态错误（routes 层映射为 400/404）。"""


def _workspace_where(workspace: str | None) -> tuple[str, list]:
    if not workspace:
        return "", []
    return (" AND COALESCE((SELECT key FROM workspaces WHERE id=co.workspace_id),'studypal') = ?",
            [workspace])


def _exam_dates(workspace: str | None, course: str | None) -> list[dict]:
    """Return upcoming exam dates (category='exam') with days_left.

    工作区判定优先用 events.workspace_id（023:11 加列），为空时回退到课程
    所属工作区；两者都为空的旧事件不绑定任何工作区，保留，否则用户在非
    默认工作区看不到自己手录的考试。
    同一课程有多场考试时只保留最近一场（days_left 最小），避免后插入的
    远期考试覆盖近期考试。
    """
    sql = """SELECT ev.course_name, ev.date, ev.title, ev.time_range,
                    CAST(julianday(ev.date) - julianday(date('now','localtime')) AS INTEGER) AS days_left
               FROM events ev
          LEFT JOIN courses co ON co.id = ev.course_id
              WHERE ev.category = 'exam' AND COALESCE(ev.date,'') <> ''
                AND julianday(ev.date) >= julianday(date('now','localtime'))"""
    args: list = []
    if workspace:
        sql += (" AND ((ev.workspace_id IS NULL AND ev.course_id IS NULL) OR COALESCE("
                "(SELECT key FROM workspaces WHERE id = COALESCE(ev.workspace_id, co.workspace_id)),"
                "'studypal') = ?)")
        args.append(workspace)
    if course:
        sql += " AND ev.course_name = ?"
        args.append(course)
    sql += " ORDER BY julianday(ev.date), ev.id LIMIT 20"
    conn = db.connect()
    try:
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    except Exception:  # noqa: BLE001 - 旧库无 events 表时复习队列仍可用
        _LOGGER.debug("exam dates unavailable", exc_info=True)
        return []
    finally:
        conn.close()
    nearest: dict[str, dict] = {}
    loose: list[dict] = []
    for row in rows:
        name = row.get("course_name") or ""
        if not name:
            loose.append(row)
            continue
        current = nearest.get(name)
        if current is None or int(row["days_left"]) < int(current["days_left"]):
            nearest[name] = row
    return sorted([*nearest.values(), *loose],
                  key=lambda row: (int(row["days_left"]), row.get("date") or ""))


def _last_reviewed_map(workspace: str | None) -> dict[int, str]:
    sql = """SELECT kp_id, MAX(occurred_at) AS last_at FROM learning_events
              WHERE kind='review_item' AND kp_id IS NOT NULL"""
    args: list = []
    if workspace:
        sql += " AND workspace_id = (SELECT id FROM workspaces WHERE key = ?)"
        args.append(workspace)
    sql += " GROUP BY kp_id"
    conn = db.connect()
    try:
        return {int(r["kp_id"]): r["last_at"] for r in conn.execute(sql, args).fetchall()}
    except Exception:  # noqa: BLE001 - 028 未应用时退化为只看 updated_at
        _LOGGER.debug("learning_events unavailable for review queue", exc_info=True)
        return {}
    finally:
        conn.close()


def _days_since(stamp: str | None) -> float | None:
    """Days between now (local) and a stored local timestamp; None when unknown."""
    if not stamp:
        return None
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT julianday(datetime('now','localtime')) - julianday(?) AS d", (str(stamp),)
        ).fetchone()
    except Exception:  # noqa: BLE001
        return None
    finally:
        conn.close()
    if not row or row["d"] is None:
        return None
    return float(row["d"])


def _reason(mastery: str, days: float | None, exam_within: int | None) -> str:
    if mastery == "未读":
        return "还没开始读"
    if exam_within is not None:
        return f"{exam_within} 天后考试"
    if days is None:
        return "很久没复习"
    return f"{int(days)} 天未复习"


def due(workspace: str | None = None, course: str | None = None, limit: int = 20) -> dict:
    """Return the fixed-rule review queue (pure query, never writes)."""
    try:
        limit = max(1, min(int(limit or 20), MAX_LIMIT))
    except (TypeError, ValueError) as exc:
        raise ReviewError("limit 必须是整数") from exc
    exams = _exam_dates(workspace, course)
    exam_within: dict[str, int] = {}
    for row in exams:
        name = row.get("course_name") or ""
        if not name:
            continue
        days = int(row["days_left"])
        if name not in exam_within or days < exam_within[name]:
            exam_within[name] = days
    last_reviewed = _last_reviewed_map(workspace)
    where, args = _workspace_where(workspace)
    sql = """SELECT kp.id, kp.name, kp.chapter, kp.mastery, kp.freq, kp.updated_at,
                    co.name AS course_name
               FROM kp JOIN courses co ON co.id = kp.course_id
              WHERE COALESCE(kp.deleted_at,'')=''""" + where
    if course:
        sql += " AND co.name = ?"
        args.append(course)
    conn = db.connect()
    try:
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()

    now_row = db.connect()
    try:
        now = now_row.execute(
            "SELECT julianday(datetime('now','localtime')) AS n").fetchone()["n"]
        ages: dict[int, float] = {}
        for kp_id, stamp in last_reviewed.items():
            if not stamp:
                continue
            row = now_row.execute("SELECT julianday(?) AS j", (str(stamp),)).fetchone()
            if row and row["j"] is not None:
                ages[kp_id] = float(now) - float(row["j"])
    finally:
        now_row.close()

    items: list[dict] = []
    for row in rows:
        mastery = row.get("mastery") or "未读"
        if mastery not in MASTERY_LADDER:
            mastery = "未读"
        last_at = last_reviewed.get(int(row["id"]))
        days = ages.get(int(row["id"]))
        if days is None:
            days = _days_since(row.get("updated_at"))
        if last_at and days is not None and days < MIN_REVIEW_GAP_DAYS:
            continue
        if mastery == "未读":
            priority, reason_days = 100, None
        else:
            gap = MASTERY_GAP_DAYS.get(mastery, MIN_REVIEW_GAP_DAYS)
            if days is not None and days <= gap:
                continue
            priority = {"读过": 70, "会用": 50, "熟练": 30}.get(mastery, 40)
            reason_days = days
        exam_in = exam_within.get(row["course_name"])
        if exam_in is not None and exam_in <= EXAM_SOON_DAYS and row.get("freq") == "高频":
            priority += EXAM_HIGH_FREQ_BONUS
        if row.get("freq") == "高频":
            priority += 5
        items.append({
            "kp_id": int(row["id"]),
            "name": row["name"],
            "course": row["course_name"],
            "chapter": row.get("chapter") or "",
            "mastery": mastery,
            "freq": row.get("freq") or "",
            "last_reviewed_at": last_at or row.get("updated_at") or "",
            "reason": _reason(mastery, reason_days, exam_in if exam_in is not None else None),
            "priority": priority,
        })
    items.sort(key=lambda it: (-it["priority"], it["mastery"], it["kp_id"]))
    return {
        "items": items[:limit],
        "total": len(items),
        "exam_dates": exams,
    }


def feedback(kp_id: int, result: str, *, workspace: str | None = None,
             source: str = "review") -> dict:
    """Record one review answer: write mastery (unless good) + learning event."""
    if result not in FEEDBACK_RESULT:
        raise ReviewError("result 必须是 again/hard/good 之一")
    kp = repo.get_kp(int(kp_id))
    if not kp:
        raise ReviewError("知识点不存在")
    target = FEEDBACK_RESULT[result]
    written = None
    if target and target != kp.get("mastery"):
        written = repo.write_mastery(int(kp_id), target, origin="review", confirmed=True)
        if not written.get("ok"):
            raise ReviewError(written.get("error") or "写回掌握度失败")
    payload = json.dumps({"result": result, "mastery_before": kp.get("mastery"),
                          "mastery_after": target or kp.get("mastery")},
                         ensure_ascii=False)
    event = None
    duplicated = False
    try:
        from . import learning  # 028 由 T3.7 提供；未应用时复习反馈仍要成功
        event, duplicated = learning.record_event(
            kind="review_item", course=kp.get("course_name"),
            kp_id=int(kp_id), workspace=workspace, payload=payload, source=source,
        )
    except Exception:  # noqa: BLE001
        _LOGGER.warning("learning event not recorded for review feedback", exc_info=True)
    current = repo.get_kp(int(kp_id)) or kp
    return {
        "ok": True,
        "kp": current.get("name"),
        "kp_id": int(kp_id),
        "mastery": current.get("mastery"),
        "changed": bool(written and written.get("changed")),
        "event": event,
        "duplicated": duplicated,
    }
