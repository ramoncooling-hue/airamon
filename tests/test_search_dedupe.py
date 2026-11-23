"""O-DATA-6: retrieval dedupe by (kind, ref_id) and explicit truncation flags."""

from __future__ import annotations

import sqlite3

import pytest

from app.agent.chat import build_personal_context_block
from app.api import routes
from app.agent_runtime.context import ContextCompiler, MAX_MEMORY
from app.memory import db, repo, search_index, sources

LONG_TEXT_HEAD = '边际成本 检索去重语义 '


def _fts5_available():
    try:
        return bool(db.sqlite_capabilities().get('fts5'))
    except Exception:
        return False


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / 'data'
    monkeypatch.setattr(db, 'APP_DIR', root / 'app')
    monkeypatch.setattr(db, 'DB_PATH', root / 'app' / 'search.db')
    monkeypatch.setattr(db, 'DATA_DIR', root)
    db.apply_schema()
    long_content = LONG_TEXT_HEAD * 120  # > 1200 chars, so the snippet is clipped
    conn = db.connect()
    try:
        course_id = conn.execute("INSERT INTO courses(name) VALUES('检索课')").lastrowid
        source_id = conn.execute(
            "INSERT INTO source_files(course_id,path,sha256,status,source_kind) "
            "VALUES(?,?,?,'indexed','textbook')",
            (course_id, '/tmp/notes.md', 'sha-notes')).lastrowid
        chunk_id = conn.execute(
            'INSERT INTO source_chunks(source_id,position,content) VALUES(?,?,?)',
            (source_id, 0, long_content)).lastrowid
        evidence_id = conn.execute(
            '''INSERT INTO evidence(source_id,locator_type,locator_start,locator_end,content,content_hash)
               VALUES(?,?,?,?,?,?)''',
            (source_id, 'line', 1, 1, '边际成本 证据原文', 'sha-evidence')).lastrowid
        kp_id = conn.execute(
            "INSERT INTO kp(course_id,name,gist,source) VALUES(?,?,?,?)",
            (course_id, '边际成本', '边际成本要点', '/tmp/notes.md')).lastrowid
        conn.commit()
    finally:
        conn.close()
    if _fts5_available():
        search_index.rebuild()
    return {
        'course': '检索课', 'course_id': course_id, 'source_id': source_id,
        'chunk_id': chunk_id, 'evidence_id': evidence_id, 'kp_id': kp_id,
    }


def _refs(rows):
    return [(row.get('kind'), str(row.get('ref_id'))) for row in rows]


def test_search_index_dedupes_duplicate_postings_in_all_paths(world, monkeypatch):
    assert _fts5_available(), 'schema 011/rebuild should provide FTS5 in this environment'
    conn = sqlite3.connect(str(db.DB_PATH))
    try:
        conn.execute("""INSERT INTO search_fts(kind,ref_id,course_name,title,body)
                        SELECT kind,ref_id,course_name,title,body FROM search_fts
                        WHERE kind='source_chunk' AND CAST(ref_id AS INTEGER)=?""", (world['chunk_id'],))
        conn.commit()
    finally:
        conn.close()
    fts_rows = search_index.search('边际成本')
    assert fts_rows
    assert len(_refs(fts_rows)) == len(set(_refs(fts_rows)))
    assert sum(1 for row in fts_rows if row['kind'] == 'source_chunk'
               and row['ref_id'] == str(world['chunk_id'])) == 1

    # LIKE fallback when the build has no FTS5 at all.
    monkeypatch.setattr(db, 'sqlite_capabilities', lambda: {'fts5': False})
    fallback_rows = search_index.search('边际成本')
    assert fallback_rows
    assert len(_refs(fallback_rows)) == len(set(_refs(fallback_rows)))

    # LIKE-over-FTS fallback when MATCH fails (corrupt/unsupported index).
    monkeypatch.setattr(db, 'sqlite_capabilities', lambda: {'fts5': True})
    real_connect = search_index.connect

    class FlakyConnection:
        def __init__(self, connection):
            self._connection = connection

        def execute(self, sql, *args):
            if ' MATCH ' in sql:
                raise sqlite3.OperationalError('malformed MATCH expression')
            return self._connection.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._connection, name)

    monkeypatch.setattr(search_index, 'connect', lambda: FlakyConnection(real_connect()))
    like_rows = search_index.search('边际成本')
    assert like_rows
    assert len(_refs(like_rows)) == len(set(_refs(like_rows)))


def test_dedupe_helper_keeps_best_ranked_row_and_limit():
    rows = [
        {'kind': 'kp', 'ref_id': '1', 'rank': 0.1},
        {'kind': 'source_chunk', 'ref_id': '1', 'rank': 0.2},
        {'kind': 'kp', 'ref_id': '1', 'rank': 9.9},
        {'kind': 'evidence', 'ref_id': '1', 'rank': 0.3},
    ]
    unique = search_index.dedupe_index_rows(rows, 10)
    assert [(row['kind'], row['ref_id']) for row in unique] == [
        ('kp', '1'), ('source_chunk', '1'), ('evidence', '1')]
    assert unique[0]['rank'] == 0.1
    assert len(search_index.dedupe_index_rows(rows, 2)) == 2


def test_combined_search_dedupes_cross_path_chunk_and_marks_truncation(world):
    hits = repo.memory_search_hits('边际成本', 10, world['course'])
    assert hits
    keys = [(hit.get('ref_kind'), str(hit.get('ref_id'))) for hit in hits]
    assert len(keys) == len(set(keys)), 'each (kind, ref_id) appears once'
    chunk_hits = [hit for hit in hits if hit.get('ref_kind') == 'source_chunk']
    assert len(chunk_hits) == 1, 'FTS hit and sources.search chunk collapse into one'
    assert chunk_hits[0]['ref_id'] == world['chunk_id']
    assert chunk_hits[0]['truncated'] is True
    assert len(chunk_hits[0]['gist']) == repo.SEARCH_GIST_LIMIT

    # Same numeric id in different kinds must not be collapsed.
    ref_pairs = {(hit.get('ref_kind'), str(hit.get('ref_id'))) for hit in hits}
    assert ('kp', str(world['kp_id'])) in ref_pairs
    assert ('evidence', str(world['evidence_id'])) in ref_pairs
    assert ('source_chunk', str(world['chunk_id'])) in ref_pairs


def test_api_search_dedupes_and_exposes_truncated_flag(world):
    payload = routes.search('边际成本', limit=10, course=world['course'])
    hits = payload['hits']
    assert payload['count'] == len(hits)
    keys = [(hit.get('ref_kind'), str(hit.get('ref_id'))) for hit in hits]
    assert len(keys) == len(set(keys))
    chunk_hit = next(hit for hit in hits if hit.get('ref_kind') == 'source_chunk')
    assert chunk_hit['truncated'] is True
    assert payload['count'] == len(hits)
    # Short snippets stay untruncated and every hit carries the explicit flag.
    assert all('truncated' in hit for hit in hits)
    assert next(hit for hit in hits if hit.get('ref_kind') == 'kp')['truncated'] is False


def test_chunks_cap_reports_explicit_truncation():
    chunks, truncated = sources._chunks('x' * (sources.MAX_SOURCE_CHUNKS * 1050 + 2000))
    assert truncated is True
    assert len(chunks) == sources.MAX_SOURCE_CHUNKS
    small, small_truncated = sources._chunks('检索内容')
    assert small_truncated is False
    assert small == ['检索内容']


def test_scan_course_surfaces_truncated_flag(tmp_path, world, monkeypatch):
    inbox = tmp_path / 'StudyPal' / '大材料课' / 'inbox'
    inbox.mkdir(parents=True)
    huge = '检索去重截断语义。' * 60000
    material = inbox / 'large.md'
    material.write_text(huge, encoding='utf-8')
    conn = db.connect()
    try:
        conn.execute("INSERT INTO courses(name) VALUES('大材料课')")
        conn.commit()
    finally:
        conn.close()
    result = sources.scan_course('大材料课', [inbox])
    assert result['truncated'] == 1
    assert result['files'][0]['truncated'] is True
    assert result['files'][0]['truncated_reason'] == 'chunk_limit'
    assert result['files'][0]['chunk_count'] == sources.MAX_SOURCE_CHUNKS
    conn = db.connect()
    try:
        stored = conn.execute(
            'SELECT COUNT(*) FROM source_chunks sc JOIN source_files sf ON sf.id=sc.source_id '
            'WHERE sf.course_id=(SELECT id FROM courses WHERE name=?) AND sc.stale=0',
            ('大材料课',)).fetchone()[0]
    finally:
        conn.close()
    assert stored == sources.MAX_SOURCE_CHUNKS


def test_context_compiler_dedupes_and_reports_truncated_lists(world, monkeypatch):
    clipped = []
    for index in range(MAX_MEMORY + 3):
        clipped.append({'id': 9000 + index, 'kind': 'kp', 'name': f'节点{index}',
                        'gist': '要点 ' * 300, 'source': '/tmp/notes.md',
                        'course_name': world['course'], 'chapter': '第一章',
                        'mastery': '', 'ref_kind': 'kp', 'ref_id': 9000 + index,
                        'truncated': index == 0})
    clipped.insert(1, dict(clipped[0]))  # the same (kind, ref_id) twice
    monkeypatch.setattr(repo, 'memory_search_hits', lambda *args, **kwargs: clipped)
    compiler = ContextCompiler()
    context = compiler.build_context(workspace_key='studypal', question='边际成本')
    memory = context['memory']
    source_ids = [item['source_id'] for item in memory]
    assert len(source_ids) == len(set(source_ids))
    assert len(memory) <= MAX_MEMORY
    assert context['truncated']['memory'] is True
    assert context['truncated']['snippets'] is True

    block = build_personal_context_block(context)
    assert '（片段已截断）' in block
    assert '上下文截断标记：' in block
    assert 'memory' in block.split('上下文截断标记：', 1)[1]
