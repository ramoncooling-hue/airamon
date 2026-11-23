"""SQLite-backed, single-worker local ingestion; retries are always explicit."""

import logging
import sqlite3
import time
from pathlib import Path
from contextlib import contextmanager
from threading import Event, Lock, RLock, Thread

from ..memory import db, repo, sources
from ..maintenance.lock import _acquire, _release
from .control import IngestCancelled, checkpoint, controlled

TERMINAL = {'waiting_review', 'succeeded', 'failed', 'cancelled', 'interrupted'}
# O-DATA-5: a running job refreshes updated_at while its worker is alive.
# ingest_jobs has no dedicated heartbeat column (019 is frozen), so updated_at
# doubles as the heartbeat; startup and the loop reap heartbeatless runs.
HEARTBEAT_INTERVAL_SECONDS = 5
HEARTBEAT_TIMEOUT_SECONDS = 30
INTERRUPTED_ERROR = '上次识别中断，请确认后重试'
HEARTBEAT_ERROR = '识别进程无心跳，已标记中断，请确认后重试'
# P1 (T2.11): a large indexing transaction holds SQLite's write lock; cancel
# must answer quickly instead of blocking for the default 5s and turning the
# API call into a 500. Three short attempts with a small busy_timeout, then a
# domain error the route maps to 409 with a retry hint.
CANCEL_LOCK_RETRIES = 3
CANCEL_LOCK_BACKOFF_SECONDS = 0.05
CANCEL_BUSY_TIMEOUT_MS = 250
CANCEL_BUSY_ERROR = '材料正在写入索引，请稍后重试取消'
logger = logging.getLogger(__name__)


class IngestBusyError(RuntimeError):
    """The database writer is busy; the caller may retry the cancel later."""


def _is_locked_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return 'database is locked' in text or 'database table is locked' in text
_lifecycle = Lock()
_stop = Event()
_wake = Event()
_thread = None
_accepting = True
_lease = None
_registration_lock = RLock()


@contextmanager
def registration():
    """Serialize archive publication/metadata registration with directory scans."""
    with _registration_lock:
        yield


def _connect():
    return db.connect()


def _effective_status(conn, value):
    """Report a reviewed job as succeeded without writing on a read path.

    The physical transition is recorded by finish() (or the worker thread's
    background reconciliation); reads must not mutate state.
    """
    if value.get('status') == 'waiting_review' and value.get('source_id') is not None:
        pending = conn.execute(
            "SELECT 1 FROM knowledge_candidates WHERE source_id=? AND status='pending' LIMIT 1",
            (value['source_id'],)).fetchone()
        if not pending:
            value['status'] = 'succeeded'
            value['stage'] = 'complete'
    return value


def get(task_id):
    """Pure read: never settles or otherwise writes a job row."""
    conn = _connect()
    try:
        row = conn.execute('''SELECT j.*,co.name AS course FROM ingest_jobs j
                              JOIN courses co ON co.id=j.course_id WHERE j.id=?''', (task_id,)).fetchone()
        if not row:
            raise ValueError('识别任务不存在')
        value = dict(row)
        value['task_id'] = value['id']
        return _effective_status(conn, value)
    finally:
        conn.close()


def finish(task_id):
    """Explicitly settle a waiting_review task after its candidates were reviewed.

    Candidate review is a write event; callers (or the worker thread) record the
    derived job status here instead of letting GET requests mutate the database.
    """
    conn = _connect()
    try:
        with conn:
            conn.execute("""UPDATE ingest_jobs SET status='succeeded',stage='complete',updated_at=datetime('now','localtime')
                WHERE id=? AND status='waiting_review' AND source_id IS NOT NULL
                AND NOT EXISTS (SELECT 1 FROM knowledge_candidates WHERE source_id=ingest_jobs.source_id AND status='pending')""", (task_id,))
    finally:
        conn.close()
    return get(task_id)


def reconcile_review_status():
    """Settle reviewed waiting_review jobs; the background write counterpart of finish()."""
    conn = _connect()
    try:
        with conn:
            changed = conn.execute("""UPDATE ingest_jobs SET status='succeeded',stage='complete',updated_at=datetime('now','localtime')
                WHERE status='waiting_review' AND source_id IS NOT NULL
                AND NOT EXISTS (SELECT 1 FROM knowledge_candidates WHERE source_id=ingest_jobs.source_id AND status='pending')""").rowcount
        return changed
    finally:
        conn.close()


def list_jobs(course=None, workspace=None):
    conn = _connect()
    try:
        rows = conn.execute('''SELECT j.id FROM ingest_jobs j JOIN courses co ON co.id=j.course_id
            WHERE (? IS NULL OR co.name=?) AND (? IS NULL OR j.workspace_key=?)
            ORDER BY j.id DESC LIMIT 100''', (course, course, workspace, workspace)).fetchall()
        return [get(row['id']) for row in rows]
    finally:
        conn.close()


@registration()
def enqueue(course, path, folder, *, digest=None, source_kind=None, source_role='', ingest_run_id=None):
    if not _accepting:
        raise ValueError('服务正在退出，识别任务已停止接收')
    path, folder = Path(path).resolve(), Path(folder).resolve()
    if path.stat().st_size > sources.MAX_BYTES:
        raise ValueError('文件超过 200 MB')
    digest = digest or sources.file_digest(path)
    metadata = sources.source_metadata(source_kind, source_role)
    conn = _connect()
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            if not _accepting:
                raise ValueError('服务正在退出，识别任务已停止接收')
            course_row = conn.execute('''SELECT co.id,COALESCE(w.key,'studypal') AS workspace_key
                FROM courses co LEFT JOIN workspaces w ON w.id=co.workspace_id WHERE co.name=?''', (course,)).fetchone()
            if not course_row:
                raise ValueError('课程不存在')
            source = conn.execute('SELECT * FROM source_files WHERE course_id=? AND path=?', (course_row['id'], str(path))).fetchone()
            if source and source_kind is None:
                metadata = sources.source_metadata(source['source_kind'], source['source_role'])
            old = conn.execute('SELECT * FROM ingest_jobs WHERE course_id=? AND sha256=?', (course_row['id'], digest)).fetchone()
            if old:
                canonical = Path(old['path'])
                try:
                    canonical_valid = canonical.is_file() and sources.file_digest(canonical) == digest
                except OSError:
                    canonical_valid = False
                old_source = conn.execute('SELECT * FROM source_files WHERE id=?', (old['source_id'],)).fetchone()
                if old['status'] in {'succeeded', 'waiting_review'}:
                    canonical_valid = canonical_valid and old_source and old_source['path'] == old['path'] \
                        and old_source['sha256'] == digest and old_source['status'] == 'indexed'
                if canonical_valid:
                    if ingest_run_id and ingest_run_id != old['ingest_run_id']:
                        conn.execute("UPDATE ingest_runs SET status='deduplicated',source_id=?,finished_at=datetime('now','localtime') WHERE id=?",
                                     (old['source_id'], ingest_run_id))
                    task_id = old['id']
                else:
                    if old['status'] == 'running':
                        raise ValueError('原材料在识别期间发生变化，请等待当前任务结束后重新扫描')
                    # The digest belongs to a historical task whose path was
                    # overwritten/deleted. Rebind that task to this valid archive.
                    if not ingest_run_id:
                        ingest_run_id = conn.execute('''INSERT INTO ingest_runs(course_id,source_kind,source_role,status)
                            VALUES(?,?,?,'queued')''', (course_row['id'], metadata['source_kind'], metadata['source_role'])).lastrowid
                    conn.execute('''INSERT INTO source_files(course_id,path,source_kind,source_role,catalog_status,ingest_run_id)
                        VALUES(?,?,?,?,?,?) ON CONFLICT(course_id,path) DO UPDATE SET
                        sha256='',status='pending',error='',source_kind=excluded.source_kind,
                        source_role=excluded.source_role,catalog_status=excluded.catalog_status,
                        ingest_run_id=excluded.ingest_run_id''', (course_row['id'], str(path), metadata['source_kind'],
                        metadata['source_role'], metadata['catalog_status'], ingest_run_id))
                    source_id = conn.execute('SELECT id FROM source_files WHERE course_id=? AND path=?',
                                             (course_row['id'], str(path))).fetchone()['id']
                    conn.execute('''UPDATE ingest_jobs SET path=?,folder=?,source_id=?,ingest_run_id=?,
                        source_kind=?,source_role=?,status='queued',stage='queued',error='',pages_done=0,
                        pages_total=0,cancel_requested=0 WHERE id=?''', (str(path), str(folder), source_id,
                        ingest_run_id, metadata['source_kind'], metadata['source_role'], old['id']))
                    task_id = old['id']
            else:
                if not ingest_run_id:
                    ingest_run_id = conn.execute('''INSERT INTO ingest_runs(course_id,source_kind,source_role,status)
                        VALUES(?,?,?,'queued')''', (course_row['id'], metadata['source_kind'], metadata['source_role'])).lastrowid
                conn.execute('''INSERT OR IGNORE INTO source_files(course_id,path,source_kind,source_role,catalog_status,ingest_run_id)
                    VALUES(?,?,?,?,?,?)''', (course_row['id'], str(path), metadata['source_kind'],
                    metadata['source_role'], metadata['catalog_status'], ingest_run_id))
                source_id = conn.execute('SELECT id FROM source_files WHERE course_id=? AND path=?', (course_row['id'], str(path))).fetchone()['id']
                task_id = conn.execute('''INSERT INTO ingest_jobs
                    (course_id,workspace_key,path,folder,sha256,source_id,ingest_run_id,source_kind,source_role)
                    VALUES(?,?,?,?,?,?,?,?,?)''', (course_row['id'], course_row['workspace_key'], str(path),
                    str(folder), digest, source_id, ingest_run_id, metadata['source_kind'], metadata['source_role'])).lastrowid
                if source and source['sha256'] == digest and source['status'] == 'indexed':
                    pending = conn.execute("SELECT 1 FROM knowledge_candidates WHERE source_id=? AND status='pending' LIMIT 1", (source_id,)).fetchone()
                    conn.execute('UPDATE ingest_jobs SET status=?,stage=? WHERE id=?',
                                 ('waiting_review' if pending else 'succeeded', 'review' if pending else 'complete', task_id))
                    conn.execute("UPDATE ingest_runs SET source_id=?,status='completed',artifact_count=1,finished_at=datetime('now','localtime') WHERE id=?", (source_id, ingest_run_id))
                else:
                    conn.execute("UPDATE ingest_runs SET source_id=?,status='queued' WHERE id=?", (source_id, ingest_run_id))
        _wake.set()
        task = get(task_id)
        task['deduplicated'] = bool(old and canonical_valid)
        return task
    finally:
        conn.close()


def _update(task_id, **fields):
    conn = _connect()
    try:
        with conn:
            conn.execute('UPDATE ingest_jobs SET ' + ','.join(f'{key}=?' for key in fields) +
                         ",updated_at=datetime('now','localtime') WHERE id=?", (*fields.values(), task_id))
    finally:
        conn.close()


def _touch_heartbeat(task_id: int) -> None:
    """Refresh the running job's heartbeat without touching terminal rows."""
    conn = _connect()
    try:
        with conn:
            conn.execute("""UPDATE ingest_jobs SET updated_at=datetime('now','localtime')
                             WHERE id=? AND status='running'""", (task_id,))
    finally:
        conn.close()


def _heartbeat_loop(task_id: int, stop_event: Event) -> None:
    lock_warning_logged = False
    while not stop_event.wait(HEARTBEAT_INTERVAL_SECONDS):
        try:
            _touch_heartbeat(task_id)
            lock_warning_logged = False
        except sqlite3.OperationalError as exc:
            if _is_locked_error(exc):
                # P3 (T2.11): long indexing transactions are expected to hold
                # the write lock. Log once per episode at WARNING; never spam
                # ERROR for a normal long write.
                if not lock_warning_logged:
                    logger.warning(
                        'ingest heartbeat delayed by a long write transaction: %s', exc)
                    lock_warning_logged = True
            else:
                logger.exception('ingest heartbeat update failed')
        except Exception:
            # A transient database failure must not kill the extraction worker.
            logger.exception('ingest heartbeat update failed')


def reconcile_stale_runs() -> int:
    """Interrupt running jobs whose worker heartbeat stopped.

    The worker refreshes updated_at every HEARTBEAT_INTERVAL_SECONDS; a running
    row that is older than HEARTBEAT_TIMEOUT_SECONDS has no live owner, so it
    must not stay forever running in the API/UI.
    """
    conn = _connect()
    try:
        with conn:
            stale = [dict(row) for row in conn.execute(
                """SELECT id,ingest_run_id FROM ingest_jobs
                    WHERE status='running'
                      AND updated_at < datetime('now','localtime',?)""",
                (f'-{HEARTBEAT_TIMEOUT_SECONDS} seconds',)).fetchall()]
            if not stale:
                return 0
            job_ids = [row['id'] for row in stale]
            run_ids = [row['ingest_run_id'] for row in stale
                       if row['ingest_run_id'] is not None]
            job_placeholders = ','.join('?' for _ in job_ids)
            conn.execute(
                f"""UPDATE ingest_jobs SET status='interrupted',stage='stopped',error=?
                     WHERE id IN ({job_placeholders})""",
                (HEARTBEAT_ERROR, *job_ids))
            if run_ids:
                run_placeholders = ','.join('?' for _ in run_ids)
                conn.execute(
                    f"""UPDATE ingest_runs SET status='interrupted',error=?,
                               finished_at=datetime('now','localtime')
                         WHERE id IN ({run_placeholders})""",
                    (HEARTBEAT_ERROR, *run_ids))
        return len(stale)
    finally:
        conn.close()


def _interrupt_ownerless(conn, reason: str) -> None:
    """Fail jobs (and their runs) whose worker died, plus ownerless running runs."""
    conn.execute(
        """UPDATE ingest_jobs SET status='interrupted',stage='stopped',error=?
            WHERE status IN ('queued','running')""", (reason,))
    conn.execute(
        """UPDATE ingest_runs SET status='interrupted',error=?,
                   finished_at=datetime('now','localtime')
            WHERE status IN ('queued','running')
              AND id IN (SELECT ingest_run_id FROM ingest_jobs
                          WHERE status='interrupted')""", (reason,))
    # A crash between creating a run and wiring its job must not leave a fake
    # running run behind after startup/stop recovery.
    conn.execute(
        """UPDATE ingest_runs SET status='interrupted',error=?,
                   finished_at=datetime('now','localtime')
            WHERE status='running'""", (reason,))


def _cancel_once(task_id):
    conn = _connect()
    try:
        # Short per-attempt busy timeout: the indexing transaction may own the
        # write lock for seconds, and this endpoint must stay responsive.
        conn.execute(f'PRAGMA busy_timeout={CANCEL_BUSY_TIMEOUT_MS}')
        with conn:
            conn.execute("UPDATE ingest_jobs SET cancel_requested=1,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END,updated_at=datetime('now','localtime') WHERE id=? AND status IN ('queued','running')", (task_id,))
            conn.execute("UPDATE ingest_runs SET status='cancelled' WHERE id IN (SELECT ingest_run_id FROM ingest_jobs WHERE id=? AND status='cancelled')", (task_id,))
        return get(task_id)
    finally:
        conn.close()


def cancel(task_id):
    """Request cancellation; retry a locked writer, then report a busy domain error."""
    last_error = None
    for attempt in range(CANCEL_LOCK_RETRIES):
        try:
            return _cancel_once(task_id)
        except sqlite3.OperationalError as exc:
            if not _is_locked_error(exc):
                raise
            last_error = exc
            if attempt < CANCEL_LOCK_RETRIES - 1:
                time.sleep(CANCEL_LOCK_BACKOFF_SECONDS)
    raise IngestBusyError(CANCEL_BUSY_ERROR) from last_error


def retry(task_id):
    if not _accepting:
        raise ValueError('服务正在退出')
    job = get(task_id)
    path = Path(job['path'])
    if not path.is_file():
        raise ValueError('原材料文件不存在，请先恢复文件')
    if sources.file_digest(path) != job['sha256']:
        raise ValueError('材料内容已变化，请重新扫描')
    conn = _connect()
    try:
        with conn:
            changed = conn.execute("""UPDATE ingest_jobs SET status='queued',stage='queued',error='',
                pages_done=0,pages_total=0,cancel_requested=0,retry_count=retry_count+1,
                updated_at=datetime('now','localtime') WHERE id=? AND status IN ('failed','cancelled','interrupted')""", (task_id,)).rowcount
            if not changed:
                raise ValueError('只有失败、取消或中断的任务可以重试')
            conn.execute("UPDATE ingest_runs SET status='queued',error='',finished_at='' WHERE id=?", (job['ingest_run_id'],))
        _wake.set()
        return get(task_id)
    finally:
        conn.close()


def scan(course, folders, *, force_ocr=False):
    result = {'queued': 0, 'unchanged': 0, 'indexed': 0, 'failed': 0, 'stale': 0, 'files': []}
    for folder in dict.fromkeys(Path(f).resolve() for f in folders):
        if _stop.is_set():
            break
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob('*')):
            if _stop.is_set():
                break
            if not path.is_file() or path.is_symlink() or any(p.startswith('.') for p in path.relative_to(folder).parts):
                continue
            if path.suffix.lower() not in sources.SUPPORTED:
                continue
            try:
                job = enqueue(course, path, folder)
                if force_ocr and job['status'] in {'failed', 'interrupted', 'cancelled'}:
                    job = retry(job['id'])
                result['queued' if job['status'] in {'queued', 'running'} else 'unchanged'] += 1
                result['files'].append({'path': str(path), 'task_id': job['id'], 'status': job['status']})
            except (OSError, ValueError) as exc:
                result['failed'] += 1
                result['files'].append({'path': str(path), 'status': 'failed', 'error': str(exc)[:300]})
        if folder.name == 'inbox':
            conn = _connect()
            try:
                with conn:
                    for row in conn.execute('''SELECT sf.id,sf.path FROM source_files sf JOIN courses co
                        ON co.id=sf.course_id WHERE co.name=? AND sf.status='indexed' ''', (course,)):
                        source_path = Path(row['path'])
                        if source_path.is_relative_to(folder) and not source_path.is_file():
                            conn.execute("UPDATE source_files SET status='stale',error='原文件已移出课程投放目录' WHERE id=?", (row['id'],))
                            result['stale'] += 1
            finally:
                conn.close()
    return result


def _progress_callback(task_id: int):
    """Progress sink preserving the last real page counts.

    P2 (T2.11): checkpoint('indexing') reports 0/0 and used to overwrite the
    page progress collected during PDF parsing, so a finished 6-page PDF showed
    0/0. Only a positive total is persisted.
    """
    def apply(stage: str, done: int, total: int) -> None:
        fields = {'stage': stage}
        if int(total or 0) > 0:
            fields['pages_done'] = int(done or 0)
            fields['pages_total'] = int(total)
        _update(task_id, **fields)
    return apply


def run_one():
    """Claim one task atomically. Also used by isolated tests without a daemon."""
    conn = _connect()
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute("SELECT 1 FROM ingest_jobs WHERE status='running'").fetchone():
                return False
            row = conn.execute("SELECT id FROM ingest_jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if not row or _stop.is_set():
                return False
            task_id = row['id']
            conn.execute("UPDATE ingest_jobs SET status='running',stage='extracting' WHERE id=?", (task_id,))
            conn.execute("UPDATE ingest_runs SET status='running',finished_at='' WHERE id=(SELECT ingest_run_id FROM ingest_jobs WHERE id=?)", (task_id,))
    finally:
        conn.close()
    job = get(task_id)
    heartbeat_stop = Event()
    heartbeat = Thread(target=_heartbeat_loop, args=(task_id, heartbeat_stop),
                       name='mindock-ingest-heartbeat', daemon=True)
    heartbeat.start()
    try:
        with controlled(lambda: _stop.is_set() or bool(get(task_id)['cancel_requested']),
                        _progress_callback(task_id)):
            checkpoint('extracting')
            if sources.file_digest(Path(job['path'])) != job['sha256']:
                raise ValueError('材料内容已变化，请重新扫描')
            # Classification is visible to the existing candidate pipeline before
            # it starts. Preserve an explicitly selected active course catalog.
            conn = _connect()
            try:
                with conn:
                    conn.execute('''UPDATE source_files SET source_kind=?,source_role=?,
                        catalog_status=CASE WHEN catalog_status='active' AND ?='course_catalog' THEN 'active' ELSE ? END,
                        ingest_run_id=? WHERE course_id=? AND path=?''',
                        (job['source_kind'], job['source_role'], job['source_kind'],
                         sources.source_metadata(job['source_kind'], job['source_role'])['catalog_status'],
                         job['ingest_run_id'], job['course_id'], job['path']))
            finally:
                conn.close()
            sources.scan_course(job['course'], [Path(job['folder'])], force_ocr=True,
                                only_path=Path(job['path']), expected_digest=job['sha256'])
            # Scan completion is a write event: materialize the derived graph
            # state here so read-only GET /graph requests never rebuild it.
            try:
                repo.sync_memory_views(job['course'])
            except Exception:
                logger.exception('graph view sync after scan failed')
            checkpoint()
            # P1 (T2.11): the write transaction above may have blocked the local
            # cancel request. Once indexing finished, honor it instead of
            # finalizing waiting_review/succeeded.
            if get(task_id)['cancel_requested']:
                raise IngestCancelled()
            conn = _connect()
            try:
                cancel_requested = conn.execute(
                    'SELECT cancel_requested FROM ingest_jobs WHERE id=?',
                    (task_id,)).fetchone()[0]
                if cancel_requested:
                    raise IngestCancelled()
                source = conn.execute('SELECT * FROM source_files WHERE course_id=? AND path=?',
                                      (job['course_id'], job['path'])).fetchone()
                if not source or source['status'] != 'indexed':
                    raise ValueError(source['error'] if source else '暂不支持该格式的文字抽取')
                pending = conn.execute("SELECT COUNT(*) FROM knowledge_candidates WHERE source_id=? AND status='pending'", (source['id'],)).fetchone()[0]
                _update(task_id, source_id=source['id'], status='waiting_review' if pending else 'succeeded', stage='review' if pending else 'complete', error='')
            finally:
                conn.close()
    except IngestCancelled:
        _update(task_id, status='interrupted' if _stop.is_set() else 'cancelled', stage='stopped')
    except Exception as exc:
        _update(task_id, status='failed', stage='failed', error=str(exc)[:500])
    finally:
        heartbeat_stop.set()
        heartbeat.join(timeout=2)
        job = get(task_id)
        conn = _connect()
        try:
            with conn:
                conn.execute("UPDATE ingest_runs SET status=?,source_id=?,error=?,artifact_count=?,finished_at=datetime('now','localtime') WHERE id=?",
                    ('completed' if job['status'] in {'waiting_review', 'succeeded'} else job['status'],
                     job['source_id'], job['error'], int(job['status'] in {'waiting_review', 'succeeded'}), job['ingest_run_id']))
        finally:
            conn.close()
    return True


def _loop():
    while not _stop.is_set():
        _wake.clear()
        try:
            reconcile_review_status()
        except Exception:
            # Candidate review state converges on the next tick; never spin.
            logger.exception('ingest review status reconciliation failed')
        try:
            reconcile_stale_runs()
        except Exception:
            logger.exception('ingest heartbeat reconciliation failed')
        try:
            if run_one():
                continue
        except Exception:
            # A transient database failure must not spin or drop queued tasks.
            logger.exception('ingest worker iteration failed')
        _wake.wait(1)


def start():
    global _thread, _accepting, _lease
    with _lifecycle:
        if _thread and _thread.is_alive():
            return
        # A second server must not recover a task owned by a live server.
        lease = Path(str(db.DB_PATH) + '.ingest.lock').open('a+b')
        try:
            _acquire(lease.fileno())
        except OSError:
            lease.close()
            raise RuntimeError('识别 worker 已由其他服务持有')
        try:
            conn = _connect()
            try:
                with conn:
                    _interrupt_ownerless(conn, INTERRUPTED_ERROR)
            finally:
                conn.close()
            _lease = lease
            _stop.clear()
            _accepting = True
            _thread = Thread(target=_loop, name='mindock-ingest', daemon=True)
            _thread.start()
        except BaseException:
            _accepting = False
            _stop.set()
            _thread = None
            _lease = None
            try:
                _release(lease.fileno())
            finally:
                lease.close()
            raise


def stop():
    global _accepting, _thread, _lease
    with _lifecycle:
        _accepting = False
        _stop.set()
        _wake.set()
        if _thread:
            _thread.join()
            _thread = None
        try:
            conn = _connect()
            try:
                with conn:
                    _interrupt_ownerless(conn, INTERRUPTED_ERROR)
            finally:
                conn.close()
        finally:
            if _lease:
                lease = _lease
                _lease = None
                try:
                    _release(lease.fileno())
                finally:
                    lease.close()
