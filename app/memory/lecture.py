"""0.6.2 旁听转录 v0（029）：旁听会话、实时字幕与课后沉淀。

分层：
    app/memory/asr.py      本地 ASR 引擎（faster-whisper，懒加载，可选依赖）
    app/memory/lecture.py  本模块：会话/片段落库、进程内流式会话编排、沉淀
    app/api/routes.py      /api/asr/*、/api/lectures/*

约定：
- 引擎缺失或模型未下载时，创建会话与读取列表仍然可用（旁听页要先显示状态
  与下载入口），只有真正推流时才会抛 EngineUnavailable / ModelMissing；
- Transcriber 非线程安全，所以每个 live 会话独占一个 StreamSession，放在
  本模块的进程内注册表里，每个会话用稳定操作锁串行化完整生命周期；
- 音频不落盘：v0 只沉淀文字（课堂记录文档 + 学习事件）。
"""

from __future__ import annotations

import hashlib
import logging
import threading
from copy import deepcopy
from datetime import datetime

from . import asr, documents, learning
from .db import connect

logger = logging.getLogger(__name__)

SESSION_SOURCES = ('mic', 'system', 'upload')
SESSION_STATUSES = ('live', 'ended', 'discarded')
SOURCE_LABELS = {'mic': '麦克风', 'system': '系统声音', 'upload': '导入音频'}
DOCUMENT_TAG = '旁听'
MAX_TITLE = 120
MAX_TEXT = 2_000
MAX_NOTES = 2_000
MAX_IDEMPOTENCY = 120
MAX_SEGMENT_CHARS = 2_000
PREVIEW_CHARS = 80
# 32 秒 16kHz int16 单声道 = 1,024,000 字节；留一倍余量给慢网络。
MAX_CHUNK_BYTES = 2_048_000
DEFAULT_LIST_LIMIT = 20
MAX_LIST_LIMIT = 100
SAMPLE_RATE = asr.SAMPLE_RATE
DEFAULT_MODEL = 'small'
MODEL_NAMES = tuple(asr.MODEL_SPECS)


class LectureError(ValueError):
    """可以原样展示给用户的旁听错误（映射为 400）。"""


class LectureNotFound(LectureError):
    """会话不存在（映射为 404）。"""


class EngineUnavailable(LectureError):
    """本机没有可用的 ASR 引擎（映射为 503，附带安装提示）。"""


class ModelMissing(LectureError):
    """引擎在但模型文件没下载（映射为 409，提示去下载）。"""


class DownloadBusy(LectureError):
    """已经有一个下载任务在跑（映射为 409）。"""


class ChunkConflict(LectureError):
    """A chunk id was already accepted with different bytes (409)."""


class ChunkAcceptedError(EngineUnavailable):
    """PCM was accepted but recognition failed; retries must not append it again."""

    def __init__(self, message, chunk_id):
        super().__init__(message)
        self.chunk_id = chunk_id


_lock = threading.Lock()
_streams: dict[int, dict] = {}
_session_locks: dict[int, threading.Lock] = {}
_finalizations: dict[int, dict] = {}
# Process-local, unbounded for the process lifetime. Ended sessions keep receipts
# for lost-response retries; discard clears receipts. No multi-worker guarantee.
_chunk_receipts: dict[int, dict[str, dict]] = {}
_download = {
    'model': '',
    'state': 'idle',
    'percent': 0.0,
    'downloaded': 0,
    'total': 0,
    'error': '',
}


# --- 小工具 ---------------------------------------------------------------


def _session_lock(session_id):
    # Keep locks after stream cleanup: callers may already be waiting on them.
    # The registry lock is released before acquiring any session operation lock.
    with _lock:
        return _session_locks.setdefault(session_id, threading.Lock())


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _text(value, limit=MAX_TEXT) -> str:
    return ' '.join(str(value or '').split()).strip()[:limit]


def _int_value(value, label, default=0):
    if value in (None, ''):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise LectureError(label + '不合法') from None


def _model_name(name) -> str:
    wanted = _text(name, 20) or DEFAULT_MODEL
    if wanted not in MODEL_NAMES:
        raise LectureError('不支持的语音模型：' + wanted)
    return wanted


def _workspace(conn, key=None) -> dict:
    wanted = _text(key, 60) or 'studypal'
    row = conn.execute(
        "SELECT id, key, name FROM workspaces WHERE key=? AND status='active'",
        (wanted,),
    ).fetchone()
    if row is None:
        raise LectureError('工作区不存在')
    return dict(row)


def _course(conn, name, workspace_id):
    wanted = _text(name, 120)
    if not wanted:
        return None
    row = conn.execute(
        'SELECT id, name, workspace_id, workspace_id AS ws FROM courses'
        ' WHERE name=? AND (? IS NULL OR workspace_id=?)',
        (wanted, workspace_id, workspace_id),
    ).fetchone()
    if row is None:
        raise LectureError('课程不存在')
    return dict(row)


def _default_title(course, when) -> str:
    day = (when or _now())[:10]
    label = _text(course, 60) or '未关联课程'
    return ('%s %s 旁听' % (day, label))[:MAX_TITLE]


def _session_select() -> str:
    return (
        'SELECT s.*, c.name AS course_name FROM lecture_sessions s'
        ' LEFT JOIN courses c ON c.id=s.course_id'
    )


def _segment_count(conn, session_id) -> int:
    row = conn.execute(
        'SELECT COUNT(*) AS total FROM lecture_segments WHERE session_id=?',
        (session_id,),
    ).fetchone()
    return int(row['total'] if row is not None else 0)


def _session_dict(conn, row) -> dict:
    item = dict(row)
    item['course'] = item.pop('course_name', None)
    item['segment_count'] = _segment_count(conn, item['id'])
    if not item.get('duration_seconds'):
        last = conn.execute(
            'SELECT COALESCE(MAX(end_ms),0) AS end_ms FROM lecture_segments'
            ' WHERE session_id=?',
            (item['id'],),
        ).fetchone()
        ended = int(last['end_ms'] or 0) if last is not None else 0
        item['duration_ms'] = ended
    else:
        item['duration_ms'] = int(item['duration_seconds']) * 1000
    return item


def _fetch_row(conn, session_id):
    row = conn.execute(_session_select() + ' WHERE s.id=?', (_int_value(session_id, '会话 id'),)).fetchone()
    if row is None:
        raise LectureNotFound('旁听会话不存在')
    return row


def _live_row(conn, session_id):
    row = _fetch_row(conn, session_id)
    if row['status'] != 'live':
        raise LectureError('旁听会话已结束')
    return row


# --- 引擎/模型状态 --------------------------------------------------------


def download_state() -> dict | None:
    with _lock:
        if _download['state'] == 'idle':
            return None
        return dict(_download)


def status() -> dict:
    """旁听页的状态：引擎是否可用、两个模型是否就绪、下载进度。"""
    models = {}
    for name in MODEL_NAMES:
        try:
            models[name] = asr.model_state(name)
        except asr.AsrUnavailable as exc:  # 未知模型不会发生，防御式
            models[name] = {'name': name, 'present': False, 'error': str(exc)}
    return {
        'engine': {
            'available': asr.engine_available(),
            'install_hint': 'pip install -r requirements-extensions.txt',
        },
        'models': models,
        'download': download_state(),
    }


def start_download(model='turbo') -> dict:
    """后台线程下载模型；不要求引擎已安装（只写模型目录）。"""
    name = _model_name(model)
    with _lock:
        if _download['state'] == 'downloading':
            raise DownloadBusy('模型正在下载中，请稍候')
        _download.update({'model': name, 'state': 'downloading', 'percent': 0.0,
                          'downloaded': 0, 'total': 0, 'error': ''})
    thread = threading.Thread(target=_download_worker, args=(name,), daemon=True)
    thread.start()
    return {'ok': True, 'started': True, 'download': download_state()}


def _download_worker(name: str) -> None:
    def progress(update):
        if not isinstance(update, dict):
            return
        with _lock:
            if _download['state'] != 'downloading':
                return
            _download['percent'] = round(float(update.get('percent') or 0), 1)
            _download['downloaded'] = int(update.get('downloaded') or 0)
            _download['total'] = int(update.get('total') or 0)

    try:
        asr.download_model(name, progress=progress)
    except Exception as exc:  # 网络/镜像/磁盘异常都要落到状态里给前端看
        logger.warning('ASR 模型下载失败：%s', exc)
        with _lock:
            _download.update({'state': 'error', 'error': str(exc)[:400]})
        return
    with _lock:
        _download.update({'state': 'done', 'percent': 100.0})


# --- 会话 -----------------------------------------------------------------


def create_session(workspace_key=None, *, course=None, title='', source='mic',
                   model=DEFAULT_MODEL, idempotency_key='') -> dict:
    clean_source = _text(source, 20) or 'mic'
    if clean_source not in SESSION_SOURCES:
        raise LectureError('声音来源不合法')
    clean_model = _model_name(model)
    key = _text(idempotency_key, MAX_IDEMPOTENCY)
    conn = connect()
    try:
        workspace = _workspace(conn, workspace_key)
        course_row = _course(conn, course, workspace['id'])
        if key:
            existing = conn.execute(
                _session_select() + ' WHERE s.idempotency_key=?', (key,)
            ).fetchone()
            if existing is not None:
                return {'session': _session_dict(conn, existing), 'duplicated': True}
        started = _now()
        clean_title = _text(title, MAX_TITLE) or _default_title(
            course_row['name'] if course_row else '', started)
        with conn:
            cursor = conn.execute(
                '''INSERT INTO lecture_sessions
                   (workspace_id,course_id,title,source,status,model,started_at,idempotency_key)
                   VALUES(?,?,?,?,'live',?,?,?)''',
                (workspace['id'], course_row['id'] if course_row else None,
                 clean_title, clean_source, clean_model, started, key))
            session_id = cursor.lastrowid
        row = _fetch_row(conn, session_id)
        return {'session': _session_dict(conn, row), 'duplicated': False}
    finally:
        conn.close()


def get_session(session_id) -> dict:
    number = _int_value(session_id, '会话 id')
    conn = connect()
    try:
        row = _fetch_row(conn, number)
        segments = [
            {'seq': int(item['seq']), 'start_ms': int(item['start_ms']),
             'end_ms': int(item['end_ms']), 'text': item['text'],
             'confidence': float(item['confidence'] or 0)}
            for item in conn.execute(
                'SELECT seq,start_ms,end_ms,text,confidence FROM lecture_segments'
                ' WHERE session_id=? ORDER BY seq', (number,))
        ]
        return {'session': _session_dict(conn, row), 'segments': segments}
    finally:
        conn.close()


def list_sessions(workspace_key=None, *, course=None, limit=DEFAULT_LIST_LIMIT) -> dict:
    clean_limit = max(1, min(_int_value(limit, 'limit', DEFAULT_LIST_LIMIT), MAX_LIST_LIMIT))
    conn = connect()
    try:
        workspace = _workspace(conn, workspace_key)
        course_row = _course(conn, course, workspace['id'])
        sql = _session_select() + " WHERE s.workspace_id=? AND s.status<>'discarded'"
        args = [workspace['id']]
        if course_row is not None:
            sql += ' AND s.course_id=?'
            args.append(course_row['id'])
        sql += ' ORDER BY s.started_at DESC, s.id DESC LIMIT ?'
        args.append(clean_limit)
        items = []
        for row in conn.execute(sql, tuple(args)):
            item = _session_dict(conn, row)
            preview = conn.execute(
                'SELECT text FROM lecture_segments WHERE session_id=? ORDER BY seq LIMIT 1',
                (row['id'],),
            ).fetchone()
            item['preview'] = (_text(preview['text'], PREVIEW_CHARS) if preview else '')
            items.append(item)
        return {'items': items}
    finally:
        conn.close()


# --- 实时推流 -------------------------------------------------------------


def _open_stream(row) -> dict:
    """为一个 live 会话创建独占流式实例（首次推流时才加载模型）。"""
    if not asr.engine_available():
        raise EngineUnavailable('语音引擎未安装：pip install -r requirements-extensions.txt')
    name = _model_name(row['model'])
    state = asr.model_state(name)
    if not state.get('present'):
        raise ModelMissing('语音模型 %s 尚未下载，请先在旁听页下载模型' % name)
    try:
        stream = asr.StreamSession(name)
    except asr.AsrUnavailable as exc:
        raise EngineUnavailable(str(exc)) from exc
    entry = {'stream': stream, 'samples': 0, 'model': name}
    _streams[int(row['id'])] = entry
    return entry


def _store_segments(conn, session_id, segments, *, start_seq=None) -> list[dict]:
    stored = []
    if not segments:
        return stored
    if start_seq is None:
        row = conn.execute(
            'SELECT COALESCE(MAX(seq),-1) AS last FROM lecture_segments WHERE session_id=?',
            (session_id,),
        ).fetchone()
        seq = int(row['last'] if row is not None else -1)
    else:
        seq = start_seq - 1
    with conn:
        for segment in segments:
            text = _text(getattr(segment, 'text', ''), MAX_SEGMENT_CHARS)
            if not text:
                continue
            seq += 1
            start = max(0, int(getattr(segment, 'start', 0) or 0))
            end = max(start, int(getattr(segment, 'end', start) or start))
            confidence = getattr(segment, 'confidence', None)
            try:
                confidence = max(0.0, min(1.0, float(confidence))) if confidence is not None else 0.0
            except (TypeError, ValueError):
                confidence = 0.0
            cursor = conn.execute(
                '''INSERT INTO lecture_segments(session_id,seq,start_ms,end_ms,text,confidence)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(session_id,seq) DO NOTHING''',
                (session_id, seq, start, end, text, confidence))
            item = {'seq': seq, 'start_ms': start, 'end_ms': end,
                    'text': text, 'confidence': confidence}
            if cursor.rowcount == 0:
                existing = conn.execute(
                    'SELECT seq,start_ms,end_ms,text,confidence FROM lecture_segments'
                    ' WHERE session_id=? AND seq=?', (session_id, seq)).fetchone()
                if dict(existing) != item:
                    raise LectureError('旁听片段序号冲突，请重试或丢弃')
            stored.append(item)
    return stored


def feed(session_id, pcm_bytes, *, chunk_id='') -> dict:
    """喂一段 int16 mono 16kHz PCM，返回本批新出的字幕片段。"""
    number = _int_value(session_id, '会话 id')
    with _session_lock(number):
        return _feed_locked(number, pcm_bytes, chunk_id=chunk_id)


def _feed_locked(number, pcm_bytes, *, chunk_id) -> dict:
    key = str(chunk_id or '').strip()
    if len(key) > MAX_IDEMPOTENCY or any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise LectureError('音频块 ID 不合法')
    receipt = None
    if key:
        digest = hashlib.sha256(pcm_bytes).digest()
        receipt = _chunk_receipts.get(number, {}).get(key)
        if receipt is not None:
            if receipt['digest'] != digest or receipt['size'] != len(pcm_bytes):
                raise ChunkConflict('音频块 ID 已用于不同的音频数据')
            if receipt.get('error') is not None:
                raise ChunkAcceptedError(receipt['error'], key)
            if receipt.get('result') is None:
                # A previous call advanced ASR but could not persist its output.
                conn = connect()
                try:
                    _store_pending(conn, number, _streams[number])
                finally:
                    conn.close()
            return deepcopy(receipt['result'])
    if not pcm_bytes:
        raise LectureError('音频块为空')
    if len(pcm_bytes) > MAX_CHUNK_BYTES:
        raise LectureError('音频块过大')
    raw_size = len(pcm_bytes)
    if len(pcm_bytes) % 2:
        pcm_bytes = pcm_bytes[:-1]
    conn = connect()
    try:
        row = _live_row(conn, number)
        if number in _finalizations:
            raise LectureError('旁听正在结束，请重试结束或丢弃')
        model = _model_name(row['model'])
    finally:
        conn.close()
    try:
        import numpy as np
    except ImportError as exc:  # numpy 属于可选扩展；缺失时给安装提示
        raise EngineUnavailable(
            '语音引擎未安装：pip install -r requirements-extensions.txt') from exc
    pcm = np.frombuffer(pcm_bytes, dtype='<i2').astype('float32') / 32768.0
    entry = _streams.get(number)
    if entry is None:
        entry = _open_stream({'id': number, 'model': model})
    elif entry['model'] != model:
        raise LectureError('旁听模型已更改，请结束或丢弃当前会话')
    conn = connect()
    try:
        stored = _store_pending(conn, number, entry)
        entry['samples'] += int(pcm.shape[0])
        entry['flushed'] = False
        duration_ms = int(entry['samples'] * 1000 / SAMPLE_RATE)
        if key:
            receipt = {'digest': digest, 'size': raw_size, 'chunk_id': key,
                       'duration_ms': duration_ms, 'result': None}
            _chunk_receipts.setdefault(number, {})[key] = receipt
        try:
            entry['pending'] = list(entry['stream'].feed(pcm))
        except Exception as exc:
            if receipt is not None:
                receipt['error'] = str(exc)
                raise ChunkAcceptedError(str(exc), key) from exc
            raise
        if receipt is not None:
            entry['pending_chunk'] = receipt
        stored.extend(_store_pending(conn, number, entry))
    finally:
        conn.close()
    if receipt is not None:
        return deepcopy(receipt['result'])
    return {'ok': True, 'segments': stored, 'duration_ms': duration_ms}


def _store_pending(conn, session_id, entry):
    # ASR has already advanced; retain its output until the transaction commits.
    pending = entry.get('pending', [])
    stored = []
    if pending:
        if 'pending_seq' not in entry:
            entry['pending_seq'] = int(conn.execute(
                'SELECT COALESCE(MAX(seq),-1)+1 FROM lecture_segments WHERE session_id=?',
                (session_id,)).fetchone()[0])
        stored = _store_segments(conn, session_id, pending, start_seq=entry['pending_seq'])
    receipt = entry.pop('pending_chunk', None)
    if receipt is not None:
        receipt['result'] = {'ok': True, 'segments': stored,
                             'duration_ms': receipt['duration_ms'],
                             'chunk_id': receipt['chunk_id'], 'accepted': True}
    entry['pending'] = []
    entry.pop('pending_seq', None)
    return stored


def _flush_stream(session_id) -> list[dict]:
    """Called only under the session operation lock; failures retain runtime state."""
    entry = _streams.get(session_id)
    if entry is None:
        return []
    conn = connect()
    try:
        stored = _store_pending(conn, session_id, entry)
        if not entry.get('flushed'):
            try:
                entry['pending'] = list(entry['stream'].flush())
            except asr.AsrUnavailable as exc:
                raise EngineUnavailable(str(exc)) from exc
            entry['flushed'] = True
        stored.extend(_store_pending(conn, session_id, entry))
        return stored
    finally:
        conn.close()


def _transcript(segments) -> str:
    lines = []
    for item in segments:
        total = max(0, int(item['start_ms'])) // 1000
        stamp = '%02d:%02d:%02d' % (total // 3600, (total % 3600) // 60, total % 60)
        lines.append('- **[%s]** %s' % (stamp, item['text']))
    return '\n'.join(lines)


def _duration_label(seconds) -> str:
    total = max(0, int(seconds))
    return '%02d:%02d:%02d' % (total // 3600, (total % 3600) // 60, total % 60)


def finalize(session_id, *, title='', make_document=True) -> dict:
    """结束旁听：补出剩余字幕，按需沉淀知识库文档 + 学习事件。"""
    number = _int_value(session_id, '会话 id')
    with _session_lock(number):
        return _finalize_locked(number, title=title, make_document=make_document)


def _finalize_result(conn, row):
    document = conn.execute(
        'SELECT id,title,version FROM knowledge_documents WHERE id=?',
        (row['document_id'],)).fetchone() if row['document_id'] else None
    return {'ok': True, 'session': _session_dict(conn, row),
            'document': dict(document) if document else None,
            'segment_count': _segment_count(conn, row['id'])}


def _finalize_locked(number, *, title, make_document):
    conn = connect()
    try:
        row = _fetch_row(conn, number)
        if row['status'] == 'ended':
            _streams.pop(number, None)
            _finalizations.pop(number, None)
            return _finalize_result(conn, row)
        if row['status'] == 'discarded':
            raise LectureError('旁听会话已丢弃')
        _flush_stream(number)
        segments = [
            {'seq': int(item['seq']), 'start_ms': int(item['start_ms']),
             'end_ms': int(item['end_ms']), 'text': item['text'],
             'confidence': float(item['confidence'] or 0)}
            for item in conn.execute(
                'SELECT seq,start_ms,end_ms,text,confidence FROM lecture_segments'
                ' WHERE session_id=? ORDER BY seq', (number,))
        ]
        workspace_key = conn.execute(
            'SELECT key FROM workspaces WHERE id=?', (row['workspace_id'],)
        ).fetchone()
        workspace_key = workspace_key['key'] if workspace_key else None
        course_name = row['course_name']
        ended = _now()
        started = row['started_at'] or ended
        try:
            duration = int((datetime.strptime(ended, '%Y-%m-%d %H:%M:%S')
                            - datetime.strptime(started, '%Y-%m-%d %H:%M:%S')).total_seconds())
        except (TypeError, ValueError):
            duration = 0
        if segments:
            last_end = max(item['end_ms'] for item in segments) // 1000
            duration = max(duration, last_end)
        clean_title = _text(title, MAX_TITLE) or row['title'] or _default_title(course_name, started)
        checkpoint = _finalizations.setdefault(number, {
            'title': clean_title, 'ended': ended, 'duration': duration,
            'make_document': bool(make_document), 'document': None,
        })
        clean_title, ended, duration = (checkpoint['title'], checkpoint['ended'], checkpoint['duration'])
        document = checkpoint['document']
        if document is None and row['document_id']:
            document = documents.get(row['document_id'])
        origin = 'lecture:%d' % number
        if document is None:
            # create() commits before reading its return value. Its initial revision
            # is a durable recovery marker if that read (or the session link) fails.
            existing = conn.execute(
                'SELECT document_id FROM document_revisions WHERE origin=? AND version=1',
                (origin,)).fetchone()
            if existing:
                document = documents.get(existing['document_id'])
        if document is None and checkpoint['make_document'] and segments and workspace_key:
            body = '\n'.join([
                '> %s · %s · 时长 %s · 来源 %s · 由旁听转录生成' % (
                    started[:10], course_name or '未关联课程',
                    _duration_label(duration), SOURCE_LABELS.get(row['source'], row['source'])),
                '',
                '## 课堂记录',
                '',
                _transcript(segments),
                '',
            ])
            document = documents.create(
                workspace_key, title=clean_title, body_md=body, kind='note',
                course_id=row['course_id'], tags=[DOCUMENT_TAG], origin=origin)
        checkpoint['document'] = document
        if document is not None and workspace_key:
            with conn:
                conn.execute('UPDATE lecture_sessions SET document_id=? WHERE id=?',
                             (document['id'], number))
            learning.record_event(
                'note_saved', workspace_key=workspace_key, course=course_name,
                document_id=document['id'],
                payload={'source': 'lecture', 'lecture_session_id': number,
                         'audio_source': row['source'], 'segments': len(segments)},
                duration_seconds=duration,
                idempotency_key='lecture-finalize-%d' % number)
        with conn:
            conn.execute(
                '''UPDATE lecture_sessions SET status='ended', ended_at=?,
                   duration_seconds=?, title=?, document_id=? WHERE id=?''',
                (ended, duration, clean_title,
                 document['id'] if document else row['document_id'], number))
        updated = _fetch_row(conn, number)
        result = _finalize_result(conn, updated)
        _streams.pop(number, None)
        _finalizations.pop(number, None)
        return result
    finally:
        conn.close()


def discard(session_id) -> dict:
    """丢弃一次旁听：软删会话（保留审计），清掉字幕，释放流式实例。"""
    number = _int_value(session_id, '会话 id')
    with _session_lock(number):
        return _discard_locked(number)


def _discard_locked(number):
    conn = connect()
    try:
        row = _fetch_row(conn, number)
        if row['status'] == 'discarded':
            return {'ok': True, 'deleted': False}
        with conn:
            conn.execute("UPDATE lecture_sessions SET status='discarded' WHERE id=?", (number,))
            conn.execute('DELETE FROM lecture_segments WHERE session_id=?', (number,))
        _streams.pop(number, None)
        _finalizations.pop(number, None)
        _chunk_receipts.pop(number, None)
        return {'ok': True, 'deleted': True}
    finally:
        conn.close()


def reset_runtime_state() -> None:
    """测试用：清空进程内流式实例与下载进度（不动数据库）。"""
    with _lock:
        _streams.clear()
        _session_locks.clear()
        _finalizations.clear()
        _chunk_receipts.clear()
        _download.update({'model': '', 'state': 'idle', 'percent': 0.0,
                          'downloaded': 0, 'total': 0, 'error': ''})
