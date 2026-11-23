"""CyberPal · 记忆层连接与建库。

设计依据：docs/ARCHITECTURE.md
记忆层是整个系统的中心，这一层只负责「记忆怎么存、怎么取」，不含业务规则。
"""

from __future__ import annotations

import sqlite3
from functools import lru_cache
from pathlib import Path

from ..runtime import ROOT, data_dir

DATA_DIR = data_dir()
APP_DIR = DATA_DIR / "app"
SCHEMA_DIR = ROOT / "app" / "schema"
DB_PATH = APP_DIR / "banxue.db"


@lru_cache(maxsize=1)
def sqlite_capabilities() -> dict:
    """Probe optional SQLite features once per process.

    FTS5 is an optimization: some embedded SQLite builds omit it, and the
    trigram tokenizer only ships with newer versions. The migration engine
    records the result in schema_capabilities; search_index uses it to avoid
    touching a virtual table that does not exist.
    """
    result = {
        'sqlite_version': sqlite3.sqlite_version,
        'fts5': False,
        'trigram': False,
    }
    conn = sqlite3.connect(':memory:')
    try:
        result['fts5'] = _probe_virtual_table(conn, 'fts5(probe_text)')
        if result['fts5']:
            result['trigram'] = _probe_virtual_table(
                conn, "fts5(probe_text, tokenize='trigram')")
    finally:
        conn.close()
    return result


def _probe_virtual_table(conn: sqlite3.Connection, definition: str) -> bool:
    """Create and drop a temporary virtual table to prove a module is usable."""
    try:
        conn.execute(f'CREATE VIRTUAL TABLE temp.capability_probe USING {definition}')
        conn.execute('DROP TABLE temp.capability_probe')
        return True
    except sqlite3.Error:
        return False


def connect() -> sqlite3.Connection:
    """打开记忆库。行用 sqlite3.Row，便于按字段名取。"""
    APP_DIR.mkdir(parents=True, exist_ok=True)
    from ..maintenance.migrations import assert_compatible, assert_no_pending_upgrade
    assert_no_pending_upgrade(DB_PATH)
    assert_compatible(DB_PATH, SCHEMA_DIR)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def apply_schema() -> list[str]:
    """Apply recorded upgrades, then refresh idempotent seed workspace bindings.

    Call before launching workers; the desktop backend holds runtime_lock for
    its lifetime. This also acquires it for standalone setup/test callers.
    """
    from ..maintenance.migrations import upgrade_database
    upgrade_database(DB_PATH, SCHEMA_DIR, legacy_upgrade=_legacy_upgrade,
                     seed_backfill=_seed_backfill)
    return [path.name for path in sorted(SCHEMA_DIR.glob("*.sql"))]


def _legacy_upgrade(conn):
    """Immutable compatibility hook; the legacy DDL now lives in 023.

    O-DATA-4 consolidated the former inline ALTER/index/backfill block into
    023_legacy_schema_consolidation.sql, so a database can be rebuilt from
    app/schema alone. This callback stays as the stable legacy identity: older
    releases stamped 018z_legacy_inline_v1 on first start, and the checksum is
    immutable, so the migration engine still records it once for fresh
    databases while every real legacy statement is applied by 023.
    """
    return None


def _seed_backfill(conn):
    conn.executemany(
        """INSERT OR IGNORE INTO workspaces
           (key,name,kind,domain,description,sort_order)
           VALUES (?,?,?,?,?,?)""",
        [
            ('studypal', 'StudyPal', 'study', 'mba', 'MBA 学习、课程材料、知识图谱与作业日历。', 0),
            ('lifepal', 'LifePal', 'life', '', '生活记录、个人计划与日常待办。', 1),
            ('workpal', 'WorkPal', 'work', '', '工作项目、会议记录与工作任务。', 2),
        ],
    )
    study_id = conn.execute("SELECT id FROM workspaces WHERE key='studypal'").fetchone()[0]
    conn.execute("UPDATE courses SET workspace_id=? WHERE workspace_id IS NULL", (study_id,))
    conn.execute("UPDATE events SET workspace_id=? WHERE workspace_id IS NULL", (study_id,))
    conn.execute("UPDATE conversations SET workspace_id=? WHERE workspace_id IS NULL", (study_id,))


def table_counts() -> dict[str, int]:
    """各表行数，用于验收核对。

    A virtual table may be unreadable on a SQLite build without the matching
    module (for example FTS5); diagnostics must degrade instead of crashing
    startup, so broken tables are omitted.
    """
    fts_available = sqlite_capabilities().get('fts5')
    conn = connect()
    try:
        counts: dict[str, int] = {}
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        for row in rows:
            name = row["name"]
            if name == 'search_fts' and not fts_available:
                continue
            try:
                counts[name] = conn.execute(
                    f'SELECT COUNT(*) AS c FROM "{name}"').fetchone()["c"]
            except sqlite3.Error:
                continue
        return counts
    finally:
        conn.close()
