"""CyberPal · 本机服务。

架构见 docs/ARCHITECTURE.md。
分层：交互层（web/）· 接口层（api/）· 智能体层（agent/）· 记忆层（memory/）

阶段 1 已就位：记忆地基 + 记忆读取/写回接口。
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread

from fastapi import Depends, FastAPI
from .static import RevalidatingStaticFiles
from fastapi.middleware.cors import CORSMiddleware

from .api.mcp_routes import verify_local_request
from .api.routes import router, scan_all_sources
from .agent_runtime.triggers import run_once as run_agent_triggers
from .logging_setup import log_path, setup_logging
from .maintenance.recovery import recover_stale_actions
from .memory.db import apply_schema, table_counts
from .memory import seed
from .memory import search_index
from .memory import db
from .memory import repo
from .runtime import ROOT
from .ingest import jobs as ingest_jobs

WEB_DIR = ROOT / "web"
# Browser origins allowed to call the local API in development. Ports stay free
# because Vite/static dev servers pick arbitrary loopback ports.
LOOPBACK_ORIGIN = r'^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$'

logger = logging.getLogger(__name__)

app = FastAPI(title="CyberPal", docs_url=None, redoc_url=None)


def configure_cors(application: FastAPI, environ: dict | None = None) -> None:
    """Restrict browser cross-origin access to this machine.

    Packaged desktop mode mounts DesktopAccessGuard instead of CORS. The
    development server only answers loopback origins; opening it to every
    origin (the historical development default) now requires MINDOCK_DEV_CORS=1
    and is logged as a warning.
    """
    environ = os.environ if environ is None else environ
    if environ.get('MINDOCK_DEV_CORS') == '1':
        logger.warning(
            'MINDOCK_DEV_CORS=1: local API CORS is open to every origin; '
            'development builds only')
        allow_origins, allow_origin_regex = ["*"], None
    else:
        allow_origins, allow_origin_regex = [], LOOPBACK_ORIGIN
    application.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_origin_regex=allow_origin_regex,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )


if os.environ.get('MINDOCK_SESSION_TOKEN'):
    from .desktop import DesktopAccessGuard
    app.add_middleware(DesktopAccessGuard, token=os.environ['MINDOCK_SESSION_TOKEN'])
else:
    configure_cors(app)

# State-changing calls under /api must come from this machine and this origin.
# The dependency is attached at include time so the shared routes.router object
# used by other entry points and tests is left untouched.
app.include_router(router, dependencies=[Depends(verify_local_request)])
_stop_scan = Event()
_scan_thread: Thread | None = None
_runtime_context = None
_last_scan_at: str | None = None
_trigger_stats: dict | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _status_counts(table: str) -> dict | None:
    """Best-effort status counters for the health probe; never raise."""
    connection = None
    try:
        connection = db.connect()
        rows = connection.execute(
            f'SELECT status, COUNT(*) FROM {table} GROUP BY status').fetchall()
        return {str(row[0]): int(row[1]) for row in rows}
    except Exception:
        return None
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def _schema_version() -> dict | None:
    """Best-effort schema fingerprint: recorded user_version plus migration count."""
    connection = None
    try:
        connection = db.connect()
        user_version = int(connection.execute('PRAGMA user_version').fetchone()[0])
        migrations = int(connection.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0])
        return {'user_version': user_version, 'migrations': migrations}
    except Exception:
        return None
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def _fts5_available() -> bool:
    """Probe FTS5 without touching the application database."""
    probe = None
    try:
        probe = sqlite3.connect(':memory:')
        row = probe.execute("SELECT sqlite_compileoption_used('ENABLE_FTS5')").fetchone()
        if row and row[0]:
            return True
        probe.execute('CREATE VIRTUAL TABLE temp.fts5_probe USING fts5(value)')
        return True
    except Exception:
        return False
    finally:
        if probe is not None:
            try:
                probe.close()
            except Exception:
                pass


def _db_size_bytes() -> int | None:
    try:
        return int(Path(db.DB_PATH).stat().st_size)
    except OSError:
        return None


def _last_backup_at() -> str | None:
    """Newest backup timestamp; paths never leave the service."""
    try:
        backups = [path for path in (db.APP_DIR / 'backups').glob('*.db') if path.is_file()]
        if not backups:
            return None
        newest = max(backups, key=lambda path: path.stat().st_mtime)
        stamp = datetime.fromtimestamp(newest.stat().st_mtime, timezone.utc)
        return stamp.isoformat(timespec='seconds')
    except OSError:
        return None
    except Exception:
        return None


def _workspace_counts() -> dict | None:
    """Per-workspace kp/documents counts; documents stay best-effort until 026 lands."""
    connection = None
    try:
        connection = db.connect()
        workspaces = connection.execute(
            'SELECT id,key FROM workspaces ORDER BY sort_order,id').fetchall()
        kp_counts = {row[0]: int(row[1]) for row in connection.execute(
            '''SELECT c.workspace_id, COUNT(*) FROM kp JOIN courses c ON c.id=kp.course_id
               GROUP BY c.workspace_id''').fetchall()}
        document_counts = None
        has_documents = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_documents'").fetchone()
        if has_documents:
            try:
                document_counts = {row[0]: int(row[1]) for row in connection.execute(
                    'SELECT workspace_id, COUNT(*) FROM knowledge_documents GROUP BY workspace_id').fetchall()}
            except sqlite3.Error:
                # 026 may still be rolling in; documents stay best-effort.
                document_counts = None
        items = []
        for row in workspaces:
            item = {'workspace': str(row[1]), 'kp': kp_counts.get(row[0], 0), 'documents': None}
            if document_counts is not None:
                item['documents'] = document_counts.get(row[0], 0)
            items.append(item)
        return {'count': len(items), 'items': items}
    except Exception:
        return None
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def _log_file_present() -> bool:
    try:
        return log_path().is_file()
    except Exception:
        return False


def _scan_loop() -> None:
    global _last_scan_at, _trigger_stats
    while not _stop_scan.is_set():
        try:
            scan_all_sources()
            _last_scan_at = _utc_now()
        except Exception:
            # A failed folder must not stop the local service; details remain on manual scan.
            logger.exception('background source scan failed')
        if _stop_scan.is_set():
            break
        try:
            _trigger_stats = run_agent_triggers()
        except Exception:
            # Personal Agent reminders are best-effort and must not stop file scanning.
            logger.exception('background agent trigger run failed')
        _stop_scan.wait(60)


@app.on_event("startup")
def startup() -> None:
    """启动即确保记忆库结构就位（幂等）。"""
    global _runtime_context
    log_file = setup_logging()
    if log_file is not None:
        logger.info('local service starting; log file: %s', log_file)
    if os.environ.get('MINDOCK_RUNTIME_LOCKED') != '1':
        from .maintenance.lock import runtime_lock
        context = runtime_lock(db.DATA_DIR)
        context.__enter__()
        _runtime_context = context
    try:
        _initialize()
    except BaseException:
        _stop_scan.set()
        if ingest_jobs._thread:
            ingest_jobs.stop()
        if _scan_thread and _scan_thread.is_alive():
            _scan_thread.join()
        if _runtime_context:
            _runtime_context.__exit__(None, None, None)
            _runtime_context = None
        raise


def _initialize() -> None:
    apply_schema()
    try:
        recovered = recover_stale_actions()
        if recovered:
            logger.warning('recovered %d stale agent action(s)', len(recovered))
    except Exception:
        logger.exception('stale agent action recovery failed')
    if not table_counts().get('courses'):
        seed.refresh()
        # The public seed loader predates workspaces; the idempotent migration
        # also backfills its freshly inserted courses into StudyPal.
        apply_schema()
    try:
        search_index.rebuild()
    except Exception:
        # FTS5 is an optimization; LIKE search remains available.
        logger.exception('search index rebuild failed; LIKE search remains available')
    # Startup is a write event: materialize derived graph views (identities,
    # legacy node meta, node structure, bridge links) here instead of on GET.
    try:
        repo.sync_memory_views()
    except Exception:
        logger.exception('derived graph materialization at startup failed')
    ingest_jobs.start()
    global _scan_thread
    _stop_scan.clear()
    if _scan_thread is None or not _scan_thread.is_alive():
        _scan_thread = Thread(target=_scan_loop, daemon=True, name='studypal-file-scan')
        _scan_thread.start()


@app.on_event('shutdown')
def shutdown() -> None:
    global _runtime_context, _scan_thread
    _stop_scan.set()
    try:
        ingest_jobs.stop()
    finally:
        if _scan_thread:
            _scan_thread.join()
            _scan_thread = None
        if _runtime_context:
            _runtime_context.__exit__(None, None, None)
            _runtime_context = None


@app.get("/api/health")
def health() -> dict:
    """Liveness probe for both shells and the web page.

    The nonce echoes MINDOCK_READY_NONCE so a desktop shell can prove this is
    the backend instance it just launched (a stale or foreign local service on
    the same port must not be loaded). The extra metrics are best effort.
    """
    return {
        "status": "ok",
        "stage": 1,
        "tables": table_counts(),
        "nonce": os.environ.get('MINDOCK_READY_NONCE', ''),
        "last_scan_at": _last_scan_at,
        "trigger_stats": _trigger_stats,
        "action_counts": _status_counts('agent_actions'),
        "ingest_queue": _status_counts('ingest_jobs'),
        "schema_version": _schema_version(),
        "fts5_available": _fts5_available(),
        "db_size_bytes": _db_size_bytes(),
        "last_backup_at": _last_backup_at(),
        "workspace_counts": _workspace_counts(),
        "log_file_present": _log_file_present(),
    }


# The static mount serves index.html for "/" (html=True). A separate "/" route
# would be shadowed by the mount, so it is intentionally not declared.
app.mount("/", RevalidatingStaticFiles(directory=WEB_DIR, html=True), name="web")
