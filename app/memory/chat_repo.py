"""CyberPal · 会话仓库。

对话是主界面，那它就必须落库：刷新不丢、换设备能找到、每句都能追到出处。
这一层只管「会话怎么存怎么取」，不含模型调用。
"""

from __future__ import annotations

import json
import re
from typing import Any

from .db import connect
from . import platform_manual


def _hydrate_manual_evidence(metadata: dict) -> dict:
    """补齐旧版本只保存 query_manual 轨迹、未保存手册结果的历史消息。"""
    if not isinstance(metadata, dict) or metadata.get('manual_results'):
        return metadata
    raw_text = str(metadata.get('raw_text') or '')
    if not re.search(r'\[手册\d+\]', raw_text):
        return metadata
    query = next(
        (str(item.get('query') or '').strip() for item in metadata.get('trace', [])
         if item.get('tool') == 'query_manual' and item.get('query')),
        '',
    )
    if not query:
        return metadata
    rows = []
    for row in platform_manual.search(query, limit=12):
        item = dict(row)
        item['citation'] = f'手册{len(rows) + 1}'
        rows.append(item)
    if not rows:
        return metadata
    hydrated = dict(metadata)
    hydrated['manual_results'] = rows
    return hydrated


def _conv_row(r) -> dict:
    return {
        "id": r["id"],
        "title": r["title"],
        "scope": r["scope"],
        "role_key": r["role_key"],
        "tier": r["tier"],
        "workspace_id": r["workspace_id"] if "workspace_id" in r.keys() else None,
        "workspace_key": r["workspace_key"] if "workspace_key" in r.keys() else "studypal",
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


def _msg_row(r) -> dict:
    def _load(s: str):
        try:
            return json.loads(s or "[]")
        except Exception:
            return []

    return {
        "id": r["id"],
        "conv_id": r["conv_id"],
        "role": r["role"],
        "content": r["content"],
        "role_name": r["role_name"],
        "scope_label": r["scope_label"],
        "steps": _load(r["steps_json"]),
        "citations": _load(r["citations_json"]),
        "writeback": _load(r["writeback_json"]),
        "engine": r["engine"],
        "error": r["error"],
        "created_at": r["created_at"],
    }


def list_conversations(limit: int = 50, workspace: str | None = None) -> list[dict]:
    """会话列表，按最近活动排。带一句摘要与轮数，列表里能认出是哪个。"""
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT c.*, COALESCE(w.key,'studypal') AS workspace_key,
                   (SELECT COUNT(*) FROM messages m WHERE m.conv_id = c.id) AS msg_count,
                   (SELECT m.content FROM messages m
                     WHERE m.conv_id = c.id AND m.role = 'user'
                     ORDER BY m.id LIMIT 1) AS first_q
              FROM conversations c LEFT JOIN workspaces w ON w.id=c.workspace_id
             WHERE (? IS NULL OR COALESCE(w.key,'studypal') = ?)
             ORDER BY c.updated_at DESC, c.id DESC
             LIMIT ?
            """,
            (workspace, workspace, limit),
        ).fetchall()
        out = []
        for r in rows:
            d = _conv_row(r)
            d["msg_count"] = r["msg_count"]
            d["summary"] = (r["first_q"] or "")[:40]
            out.append(d)
        return out
    finally:
        conn.close()


def create_conversation(title: str = "新对话", scope: str = "*",
                        role_key: str = "laoba", tier: str = "fast",
                        workspace: str = 'studypal') -> dict:
    conn = connect()
    try:
        wid = conn.execute("SELECT id FROM workspaces WHERE key=?", (workspace,)).fetchone()
        if not wid:
            raise ValueError('工作区不存在')
        cur = conn.execute(
            "INSERT INTO conversations (title, scope, role_key, tier, workspace_id) VALUES (?,?,?,?,?)",
            (title, scope, role_key, tier, wid['id']),
        )
        conn.commit()
        r = conn.execute("""SELECT c.*, COALESCE(w.key,'studypal') AS workspace_key
                              FROM conversations c LEFT JOIN workspaces w ON w.id=c.workspace_id
                              WHERE c.id=?""", (cur.lastrowid,)).fetchone()
        return _conv_row(r)
    finally:
        conn.close()


def get_conversation(conv_id: int) -> dict | None:
    conn = connect()
    try:
        r = conn.execute("""SELECT c.*, COALESCE(w.key,'studypal') AS workspace_key
                              FROM conversations c LEFT JOIN workspaces w ON w.id=c.workspace_id
                              WHERE c.id=?""", (conv_id,)).fetchone()
        if not r:
            return None
        d = _conv_row(r)
        d["messages"] = [
            _msg_row(m)
            for m in conn.execute(
                "SELECT * FROM messages WHERE conv_id = ? ORDER BY id", (conv_id,)
            )
        ]
        evidence = {row['message_id']: _hydrate_manual_evidence(json.loads(row['metadata_json'])) for row in conn.execute(
            'SELECT e.* FROM message_evidence e JOIN messages m ON m.id=e.message_id WHERE m.conv_id=?', (conv_id,)
        )}
        for message in d['messages']:
            message['metadata'] = evidence.get(message['id'], {})
        return d
    finally:
        conn.close()


def update_conversation(conv_id: int, patch: dict) -> dict | None:
    fields, values = [], []
    for k in ("title", "scope", "role_key", "tier", "workspace_id"):
        if k in (patch or {}):
            fields.append(f"{k} = ?")
            values.append(patch[k])
    if not fields:
        return get_conversation(conv_id)
    values.append(conv_id)
    conn = connect()
    try:
        conn.execute(
            f"UPDATE conversations SET {', '.join(fields)}, "
            f"updated_at = datetime('now','localtime') WHERE id = ?",
            values,
        )
        conn.commit()
        return get_conversation(conv_id)
    finally:
        conn.close()


def delete_conversation(conv_id: int) -> bool:
    conn = connect()
    try:
        cur = conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def add_message(conv_id: int, role: str, content: str, *,
                role_name: str = "", scope_label: str = "",
                steps: list | None = None, citations: list | None = None,
                writeback: list | None = None, engine: str = "local",
                error: str = "", metadata: dict | None = None) -> dict:
    """追加一条消息并顶起会话的 updated_at。"""
    conn = connect()
    try:
        cur = conn.execute(
            """INSERT INTO messages
               (conv_id, role, content, role_name, scope_label,
                steps_json, citations_json, writeback_json, engine, error)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                conv_id, role, content, role_name, scope_label,
                json.dumps(steps or [], ensure_ascii=False),
                json.dumps(citations or [], ensure_ascii=False),
                json.dumps(writeback or [], ensure_ascii=False),
                engine, error,
            ),
        )
        if metadata:
            conn.execute('INSERT INTO message_evidence(message_id, metadata_json) VALUES(?,?)',
                         (cur.lastrowid, json.dumps(metadata, ensure_ascii=False)))
        conn.execute(
            "UPDATE conversations SET updated_at = datetime('now','localtime') WHERE id = ?",
            (conv_id,),
        )
        conn.commit()
        r = conn.execute("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)).fetchone()
        return {**_msg_row(r), 'metadata': metadata or {}}
    finally:
        conn.close()


def autotitle(conv_id: int, first_question: str) -> None:
    """会话标题默认是「新对话」，第一句话进来时替换成问题摘要。"""
    t = (first_question or "").strip().replace("\n", " ")[:24]
    if not t:
        return
    conn = connect()
    try:
        conn.execute(
            "UPDATE conversations SET title = ? WHERE id = ? AND title = '新对话'",
            (t, conv_id),
        )
        conn.commit()
    finally:
        conn.close()


def stats() -> dict[str, Any]:
    conn = connect()
    try:
        c = conn.execute("SELECT COUNT(*) AS n FROM conversations").fetchone()["n"]
        m = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
        by_engine = {
            r["engine"]: r["n"]
            for r in conn.execute(
                "SELECT engine, COUNT(*) AS n FROM messages WHERE role='assistant' GROUP BY engine"
            )
        }
        return {"conversations": c, "messages": m, "by_engine": by_engine}
    finally:
        conn.close()
