"""Rebuildable sqlite-vec experiment. SQLite records remain authoritative."""
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import urllib.parse
import urllib.request

from . import db

MAX_TEXT = 6000
MAX_RECORDS = 50000


def index_path(workspace):
    key = hashlib.sha256(workspace.encode()).hexdigest()[:24]
    return db.APP_DIR / 'vector-index' / (key + '.db')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _vectors(values, count, dimension=None):
    if not isinstance(values, list) or len(values) != count:
        raise ValueError('Embedding 返回数量不符')
    normalized = []
    for v in values:
        if not isinstance(v, list) or not 1 <= len(v) <= 4096 or (dimension and len(v) != dimension):
            raise ValueError('Embedding 维度不符')
        if any(type(x) not in (int, float) or not math.isfinite(x) for x in v):
            raise ValueError('Embedding 数值无效')
        norm = math.sqrt(sum(x * x for x in v))
        if not math.isfinite(norm) or norm < 1e-12:
            raise ValueError('Embedding 向量为空或溢出')
        dimension = len(v)
        normalized.append([float(x) / norm for x in v])
    return normalized


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('本地 Embedding 不接受跳转')


class LocalEmbedding:
    """Explicit local OpenAI-compatible embedding endpoint; never reuse chat keys."""
    def __init__(self, endpoint, model):
        url = urllib.parse.urlsplit(endpoint)
        if (url.scheme != 'http' or url.hostname not in {'127.0.0.1', '::1'}
                or url.username or url.password or url.query or url.fragment
                or not url.path.endswith('/embeddings') or not model.strip()):
            raise ValueError('请使用明确的本机回环 Embedding 地址和模型名称')
        self.endpoint, self.model = endpoint, model
        self.identity = _digest({'endpoint': endpoint, 'model': model, 'max_text': MAX_TEXT})
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def embed(self, texts):
        payload = json.dumps({'model': self.model, 'input': [s[:MAX_TEXT] for s in texts],
                              'encoding_format': 'float'}).encode()
        request = urllib.request.Request(self.endpoint, data=payload, headers={'Content-Type': 'application/json'})
        with self.opener.open(request, timeout=20) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError('Embedding 响应过大')
        rows = json.loads(raw).get('data', [])
        if len(rows) != len(texts) or sorted(r.get('index', -1) for r in rows) != list(range(len(texts))):
            raise ValueError('Embedding 返回条目不完整')
        return [r['embedding'] for r in sorted(rows, key=lambda r: r['index'])]


def _corpus(workspace, course=None):
    """Read current scoped records. Hash full rows, embed only bounded excerpts."""
    conn = db.connect()
    try:
        conn.execute('BEGIN')
        row = conn.execute('SELECT id FROM workspaces WHERE key=?', (workspace,)).fetchone()
        if not row:
            raise ValueError('工作区不存在')
        wid = row['id']
        result = []
        kp_rows = conn.execute('''SELECT k.id,k.name,k.gist,k.detail,k.source,c.id course_id,c.name course_name
            FROM kp k JOIN courses c ON c.id=k.course_id
            WHERE COALESCE(k.deleted_at,'')='' AND c.workspace_id=?
              AND (? IS NULL OR c.name=?)''', (wid, course, course))
        for r in kp_rows:
            result.append({'kind': 'kp', 'id': r['id'], 'course_id': r['course_id'],
                           'course_name': r['course_name'], 'name': r['name'], 'source': r['source'],
                           'text': '\n'.join(str(r[k] or '') for k in ('name', 'gist', 'detail'))})
        for table, kind in [('source_chunks', 'file'), ('evidence', 'evidence')]:
            # table names are constants above, never supplied by callers/models.
            rows = conn.execute(f'''SELECT r.id,r.content,s.path,s.id source_id,s.sha256,
                c.id course_id,c.name course_name FROM {table} r
                JOIN source_files s ON s.id=r.source_id JOIN courses c ON c.id=s.course_id
                WHERE r.stale=0 AND s.status='indexed' AND s.source_kind!='development_doc'
                  AND c.workspace_id=? AND (? IS NULL OR c.name=?)''', (wid, course, course))
            for r in rows:
                result.append({'kind': kind, 'id': r['id'], 'course_id': r['course_id'],
                               'course_name': r['course_name'], 'name': Path(r['path']).name,
                               'source': r['path'], 'source_id': r['source_id'], 'source_sha256': r['sha256'],
                               'text': r['content']})
        if course is None:
            for r in conn.execute("SELECT id,title,body,path FROM workspace_items WHERE workspace_id=? AND status!='deleted'", (wid,)):
                result.append({'kind': 'workspace_item', 'id': r['id'], 'course_id': 0,
                               'course_name': '', 'name': r['title'], 'source': r['path'],
                               'text': r['title'] + '\n' + r['body']})
        if len(result) > MAX_RECORDS:
            raise ValueError('实验索引暂限 50000 条记录')
        return [r for r in result if r['text'].strip()]
    finally:
        conn.close()


def _connect(path, readonly=False):
    import sqlite_vec
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) if readonly else sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except BaseException:
        conn.close()
        raise
    return conn


def _database_identity():
    return _digest(str(db.DB_PATH.resolve()))


def rebuild(workspace, embedder):
    records = _corpus(workspace)
    target = index_path(workspace)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.build-', suffix='.db', dir=target.parent)
    os.close(fd)
    temporary = Path(name)
    conn = None
    try:
        conn = _connect(temporary)
        conn.execute('CREATE TABLE manifest (value TEXT NOT NULL)')
        conn.execute('CREATE TABLE records (id INTEGER PRIMARY KEY, kind TEXT, ref_id INTEGER, digest TEXT)')
        dimension = None
        for offset in range(0, len(records), 16):
            batch = records[offset:offset + 16]
            vectors = _vectors(embedder.embed([r['text'][:MAX_TEXT] for r in batch]), len(batch), dimension)
            if dimension is None:
                dimension = len(vectors[0])
                conn.execute(f'''CREATE VIRTUAL TABLE vectors USING vec0(
                    embedding float[{dimension}] distance_metric=cosine,
                    workspace TEXT PARTITION KEY, course_id INTEGER)''')
            for position, (record, vector) in enumerate(zip(batch, vectors), offset + 1):
                conn.execute('INSERT INTO records VALUES(?,?,?,?)',
                             (position, record['kind'], record['id'], _digest(record)))
                conn.execute('INSERT INTO vectors(rowid,embedding,workspace,course_id) VALUES(?,?,?,?)',
                             (position, json.dumps(vector), workspace, record['course_id']))
        manifest = {'format': 1, 'workspace': workspace, 'model': embedder.identity,
                    'dimension': dimension, 'count': len(records), 'database': _database_identity()}
        conn.execute('INSERT INTO manifest VALUES(?)', (json.dumps(manifest),))
        conn.commit()
        conn.close()
        conn = None
        with temporary.open('rb') as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        return {'ok': True, **manifest}
    finally:
        if conn:
            conn.close()
        temporary.unlink(missing_ok=True)


def search(query, workspace, embedder, course=None, limit=6):
    target = index_path(workspace)
    if not target.exists():
        return {'status': 'not_built', 'hits': [], 'stale_count': 0}
    conn = _connect(target, readonly=True)
    try:
        meta = json.loads(conn.execute('SELECT value FROM manifest').fetchone()[0])
        if (meta.get('format') != 1 or meta['model'] != embedder.identity or meta['workspace'] != workspace
                or meta['database'] != _database_identity()):
            return {'status': 'rebuild_required', 'hits': [], 'stale_count': 0}
        if not meta['count']:
            return {'status': 'ready', 'hits': [], 'stale_count': 0}
        vector = _vectors(embedder.embed([query[:MAX_TEXT]]), 1)[0]
        if len(vector) != meta['dimension']:
            return {'status': 'rebuild_required', 'hits': [], 'stale_count': 0}
        limit = max(1, min(int(limit), 50))
        # Oversampling lets a few stale entries be removed without losing all
        # recall. Bulk edits require a rebuild, never returning stale contents.
        sql = 'SELECT rowid,distance FROM vectors WHERE embedding MATCH ? AND k=? AND workspace=?'
        args = [json.dumps(vector), min(meta['count'], max(64, limit * 4)), workspace]
        if course is not None:
            primary = db.connect()
            try:
                course_row = primary.execute('''SELECT c.id FROM courses c
                    JOIN workspaces w ON w.id=c.workspace_id WHERE c.name=? AND w.key=?''',
                    (course, workspace)).fetchone()
            finally:
                primary.close()
            if not course_row:
                return {'status': 'ready', 'hits': [], 'stale_count': 0}
            sql += ' AND course_id=?'
            args.append(course_row['id'])
        neighbors = conn.execute(sql, args).fetchall()
        # Hydrate after embedding/network and KNN work so deletions or scope
        # changes made while those operations ran cannot be returned as facts.
        current = {(r['kind'], r['id']): r for r in _corpus(workspace, course)}
        hits, stale = [], 0
        for row in neighbors:
            indexed = conn.execute('SELECT * FROM records WHERE id=?', (row['rowid'],)).fetchone()
            record = current.get((indexed['kind'], indexed['ref_id']))
            if not record or _digest(record) != indexed['digest']:
                stale += 1
                continue
            if len(hits) < limit:
                hits.append({k: v for k, v in record.items() if k != 'text'} | {
                    'gist': record['text'][:1200], 'distance': row['distance'],
                    'content_digest': _digest(record)})
        return {'status': 'ready', 'hits': hits, 'stale_count': stale}
    finally:
        conn.close()


def status():
    return {'installed': importlib.util.find_spec('sqlite_vec') is not None,
            'mode': 'experiment', 'chat_enabled': False,
            'indexes': len(list((db.APP_DIR / 'vector-index').glob('*.db')))}
