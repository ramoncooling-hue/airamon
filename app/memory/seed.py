"""Merge the portable starter library while preserving personal records."""

from __future__ import annotations

import sqlite3
import json
import threading
from datetime import datetime
from pathlib import Path

from .db import APP_DIR, DB_PATH, ROOT, connect

LOCK = threading.Lock()
SEED_DIR = ROOT / 'seed'


def refresh() -> dict:
    with LOCK:
        if not (SEED_DIR / 'dash_courses.json').exists():
            raise RuntimeError('安装包缺少 seed/dash_courses.json')
        manifest_path = SEED_DIR / 'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {}
        backup_dir = APP_DIR / 'backups'
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"before-seed-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.db"
        source = connect()
        target = sqlite3.connect(backup)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        # The backend owns the lifetime runtime lock. Its seed writer must stay
        # in-process, including in source installs and HTTP worker threads.
        from scripts import load_memory
        previous_data = load_memory.DATA
        load_memory.DATA = SEED_DIR
        try:
            load_memory.main([])
        except Exception as exc:
            raise RuntimeError(f'刷新失败，刷新前备份保存在 {backup}：{exc}') from exc
        finally:
            load_memory.DATA = previous_data
        return {'ok': True, 'backup': str(backup), 'version': manifest.get('version', '未标记'),
                'summary': '公共课程、知识点、日程与通知已增量刷新'}
