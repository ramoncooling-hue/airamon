"""WorkPal 一键导入 Obsidian Wiki：解析迁移、幂等建图、任务状态机与 API 契约。

覆盖：
- import_pages：自动建 WorkPal 课程 + inbox、source/wiki_page 元数据、
  kp/source/evidence/kp_edges 数量、幂等、progress、dry-run、参数校验；
- 检索：wiki 页面默认可见（memory_search_hits 不传 include_dev），
  development_doc 仍默认排除，ON CONFLICT 把旧 development_doc 行升级为 wiki_page；
- vault_candidates：obsidian.json 读取、wiki 子目录优先、坏 JSON 不抛；
- 状态机：idle/running/done、单并发、异常落 error；
- API：GET status（含 candidates）、POST 202/400/409。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes
from app.memory import db, repo, sources, wiki_import

COURSE = 'AI工作系统'
WIKI_QUERY = '边际贡献率'
DEV_QUERY = '开发文档专属术语'

PAGE_A = (
    '---\ntitle: 甲概念\ntype: concept\ntags: [成本, 管理]\nsource: raw/a.md\n'
    'knowledge_status: current\n---\n## 摘要\n边际贡献率是核心口径。\n\n'
    '## 详细内容\n参见 [[B]] 与 [[Missing]]。\n'
)
PAGE_B = (
    '---\ntitle: 乙方法\ntype: guide\ntags: [工具]\n---\n'
    '## 摘要\n支撑甲概念。\n\n## 详细内容\n[[C]]\n'
)
PAGE_C = (
    '---\ntitle: 丙模型\ntype: overview\n---\n'
    '## 摘要\n顶层模型。\n\n## 详细内容\n[[A]]\n'
)
PAGES = {'Ch1/A.md': PAGE_A, 'Ch1/B.md': PAGE_B, 'Ch1/C.md': PAGE_C}


def _write_pages(wiki_root: Path) -> None:
    for relative, text in PAGES.items():
        path = wiki_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')


def _course_id(workspace: str = 'workpal', name: str = COURSE) -> int | None:
    conn = db.connect()
    try:
        row = conn.execute(
            """SELECT c.id FROM courses c JOIN workspaces w ON w.id=c.workspace_id
                WHERE c.name=? AND w.key=?""", (name, workspace)).fetchone()
        return int(row['id']) if row else None
    finally:
        conn.close()


def _scalar(sql: str, params=()) -> int:
    conn = db.connect()
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def _rows(sql: str, params=()) -> list[dict]:
    conn = db.connect()
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _wait_done(timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    state = wiki_import.status()
    while state['state'] in {'idle', 'running'} and time.time() < deadline:
        time.sleep(0.02)
        state = wiki_import.status()
    return state


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    monkeypatch.setattr(db, 'DATA_DIR', data)
    monkeypatch.setattr(db, 'APP_DIR', data / 'app')
    monkeypatch.setattr(db, 'DB_PATH', data / 'app' / 'wiki.db')
    db.apply_schema()
    vault = tmp_path / 'vault'
    wiki = vault / 'wiki'
    _write_pages(wiki)
    obsidian = tmp_path / 'obsidian.json'
    obsidian.write_text(json.dumps({'vaults': {'abc': {'path': str(vault)}}}),
                        encoding='utf-8')
    wiki_import.reset_job_state()
    yield {'data': data, 'vault': vault, 'wiki': wiki, 'obsidian': obsidian,
           'tmp': tmp_path}
    wiki_import.reset_job_state()


def _api_client():
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app, base_url='http://127.0.0.1')


# --- 导入建图 ---------------------------------------------------------------


def test_import_creates_workpal_course_and_graph(env):
    result = wiki_import.import_pages(env['wiki'], allow_api=True)
    assert result['workspace'] == 'workpal'
    assert result['course'] == COURSE
    assert result['pages'] == 3
    assert result['excluded_markdown'] == 0
    assert result['copied'] == 3
    assert result['sources_created'] == 3
    assert result['sources_updated'] == 0
    assert result['nodes_created'] == 3
    assert result['nodes_updated'] == 0
    assert result['edges_created'] == 3
    assert result['unresolved_links'] == 1
    assert result['dry_run'] is False
    assert Path(result['backup']).is_file()
    assert isinstance(result['audit_run_id'], int) and result['audit_run_id'] > 0

    course_id = _course_id()
    assert course_id is not None
    assert (env['data'] / 'WorkPal' / COURSE / 'inbox').is_dir()
    assert _scalar('SELECT COUNT(*) FROM courses') == 1
    assert _scalar('SELECT COUNT(*) FROM source_files') == 3
    assert _scalar('SELECT COUNT(*) FROM source_chunks') == 3
    assert _scalar('SELECT COUNT(*) FROM evidence') == 3
    assert _scalar('SELECT COUNT(*) FROM kp') == 3
    assert _scalar('SELECT COUNT(*) FROM kp_edges') == 3
    assert _scalar('SELECT COUNT(*) FROM knowledge_node_meta') == 3
    assert _scalar("SELECT COUNT(*) FROM knowledge_node_meta WHERE domain='work'") == 3

    rows = _rows('SELECT path, source_kind, source_role, status FROM source_files')
    assert {row['source_kind'] for row in rows} == {'wiki_page'}
    assert {row['source_role'] for row in rows} == {'wiki_page'}
    assert {row['status'] for row in rows} == {'indexed'}
    for row in rows:
        assert row['path'].startswith(str(env['data'] / 'WorkPal' / COURSE / 'wiki'))
        assert Path(row['path']).is_file()
    audit = _rows("SELECT * FROM ingest_runs ORDER BY id DESC LIMIT 1")[0]
    assert audit['source_kind'] == 'maintenance_import'
    assert audit['source_role'] == 'owner_only'
    assert audit['extractor'] == 'script:import_wiki_workpal'
    assert audit['course_id'] == course_id
    assert audit['status'] == 'completed'
    assert audit['artifact_count'] == 6  # 3 nodes_created + 3 edges_created

    meta = _rows('SELECT attributes_json, status FROM knowledge_node_meta ORDER BY kp_id')
    attributes = json.loads(meta[0]['attributes_json'])
    assert attributes['wiki_rel'] in PAGES
    assert attributes['wiki_type'] in {'concept', 'guide', 'overview'}


def test_import_is_idempotent(env):
    first = wiki_import.import_pages(env['wiki'], allow_api=True)
    counts = {table: _scalar('SELECT COUNT(*) FROM ' + table)
              for table in ('source_files', 'source_chunks', 'evidence', 'kp',
                            'kp_edges', 'knowledge_node_meta')}
    second = wiki_import.import_pages(env['wiki'], allow_api=True)
    assert second['pages'] == first['pages'] == 3
    assert second['copied'] == 3
    assert second['sources_created'] == 0
    assert second['sources_updated'] == 3
    assert second['nodes_created'] == 0
    assert second['nodes_updated'] == 3
    assert second['edges_created'] == 0
    assert second['unresolved_links'] == 1
    for table, expected in counts.items():
        assert _scalar('SELECT COUNT(*) FROM ' + table) == expected


def test_vault_root_prefers_compiled_wiki_and_excludes_private_paths(env):
    (env['vault'] / 'raw.md').write_text('# Not compiled', encoding='utf-8')
    for relative in ('.obsidian/private.md', '.trash/deleted.md', 'node_modules/readme.md'):
        path = env['wiki'] / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# Not knowledge', encoding='utf-8')
    outside = env['tmp'] / 'outside.md'
    outside.write_text('# Private', encoding='utf-8')
    (env['wiki'] / 'escape.md').symlink_to(outside)
    (env['wiki'] / '_governance').mkdir()
    (env['wiki'] / 'HEALTH_REPORT_2026-10.md').write_text('# health', encoding='utf-8')
    (env['wiki'] / 'INDEX.md').write_text('# index', encoding='utf-8')
    (env['wiki'] / 'Ch1/Ch1-INDEX.md').write_text('# chapter index', encoding='utf-8')
    result = wiki_import.import_pages(env['vault'], apply=False)
    assert result['wiki_root'] == str(env['wiki'].resolve())
    assert result['pages'] == 3
    assert result['excluded_markdown'] == 3


def test_plain_vault_preserves_index_notes_and_unicode_chapters(env):
    root = env['tmp'] / 'ordinary'
    for relative in ('INDEX.md', '项目/概念.MD', '生活/概念.md'):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# ' + relative, encoding='utf-8')
    result = wiki_import.import_pages(root, '个人笔记', allow_api=True)
    assert result['pages'] == 3
    chapters = _rows("SELECT chapter_key FROM kp WHERE chapter IN ('项目','生活')")
    assert len({row['chapter_key'] for row in chapters}) == 2


def test_ordinary_vault_named_wiki_keeps_index_and_index_suffixed_notes(env):
    root = env['tmp'] / 'ordinary' / 'wiki'
    for relative in ('INDEX.md', 'Business/Business-INDEX.md', 'Business/RealNote.md'):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# ' + relative, encoding='utf-8')
    result = wiki_import.import_pages(root, '普通知识库', apply=False)
    assert result['pages'] == 3


def test_reimport_updates_by_source_identity_and_preserves_mastery(env):
    wiki_import.import_pages(env['wiki'], allow_api=True)
    before = _rows("SELECT id FROM kp WHERE record_id='wiki:Ch1/A.md'")[0]['id']
    conn = db.connect()
    with conn:
        conn.execute("UPDATE kp SET mastery='会用' WHERE id=?", (before,))
    conn.close()
    (env['wiki'] / 'Ch1/A.md').write_text(
        '# 甲概念改名\n\n## 摘要\n新的内容。\n\n## 关联\n[[C]]', encoding='utf-8')
    result = wiki_import.import_pages(env['wiki'], allow_api=True)
    row = _rows('SELECT * FROM kp WHERE id=?', (before,))[0]
    assert result['nodes_created'] == 0
    assert row['name'] == '甲概念改名'
    assert row['gist'] == '新的内容。'
    assert row['mastery'] == '会用'
    targets = _rows('SELECT k.record_id FROM kp_edges e JOIN kp k ON k.id=e.to_kp WHERE e.from_kp=?', (before,))
    assert [row['record_id'] for row in targets] == ['wiki:Ch1/C.md']


def test_wiki_update_keeps_old_evidence_stale_and_creates_new_snapshot(env):
    wiki_import.import_pages(env['wiki'], allow_api=True)
    kp_id = _rows("SELECT id FROM kp WHERE record_id='wiki:Ch1/A.md'")[0]['id']
    before = _rows('''SELECT e.id,e.content,e.stale,e.snapshot_id FROM evidence e
                       JOIN source_files sf ON sf.id=e.source_id
                       WHERE sf.path LIKE '%/Ch1/A.md' ORDER BY e.id''')
    assert before and all(row['stale'] == 0 for row in before)
    old_evidence_ids = {row['id'] for row in before}

    (env['wiki'] / 'Ch1/A.md').write_text(
        '# 甲概念\n\n## 摘要\n更新后的新证据。\n\n## 详细内容\n新版本正文。\n',
        encoding='utf-8')
    wiki_import.import_pages(env['wiki'], allow_api=True)

    current = _rows('''SELECT e.id,e.content,e.stale,e.snapshot_id FROM evidence e
                        JOIN source_files sf ON sf.id=e.source_id
                        WHERE sf.path LIKE '%/Ch1/A.md' ORDER BY e.id''')
    assert old_evidence_ids <= {row['id'] for row in current}
    assert all(row['stale'] == 1 for row in current if row['id'] in old_evidence_ids)
    active = [row for row in current if row['stale'] == 0]
    assert active and all(row['snapshot_id'] is not None for row in active)
    assert any('更新后的新证据' in row['content'] for row in active)
    assert _scalar('SELECT COUNT(*) FROM source_snapshots WHERE source_id=(SELECT id FROM source_files WHERE path LIKE "%/Ch1/A.md")') == 2
    assert _scalar('SELECT COUNT(*) FROM kp_evidence WHERE kp_id=?', (kp_id,)) >= 2


def test_new_wiki_nodes_get_editable_documents_and_reimport_preserves_user_document(env):
    wiki_import.import_pages(env['wiki'], allow_api=True)
    node = _rows("SELECT id,name FROM kp WHERE record_id='wiki:Ch1/A.md'")[0]
    document = _rows('SELECT * FROM knowledge_documents WHERE kp_id=?', (node['id'],))
    assert len(document) == 1
    assert document[0]['kind'] == 'concept'
    assert '边际贡献率是核心口径' in document[0]['body_md']
    assert _scalar('SELECT COUNT(*) FROM document_revisions WHERE document_id=?', (document[0]['id'],)) == 1

    from app.memory import documents
    documents.update(document[0]['id'], body_md='用户补充的长期笔记', origin='manual-test')
    (env['wiki'] / 'Ch1/A.md').write_text(
        '# 甲概念\n\n## 摘要\n更新的 Wiki 摘要。\n\n## 详细内容\n更新的 Wiki 正文。\n',
        encoding='utf-8')
    wiki_import.import_pages(env['wiki'], allow_api=True)

    after = _rows('SELECT * FROM knowledge_documents WHERE kp_id=?', (node['id'],))[0]
    assert after['body_md'] == '用户补充的长期笔记'
    assert after['version'] == 2
    assert _scalar('SELECT COUNT(*) FROM document_revisions WHERE document_id=?', (after['id'],)) == 2


def test_wiki_reimport_does_not_overwrite_fields_edited_in_mindock(env):
    wiki_import.import_pages(env['wiki'], allow_api=True)
    node = _rows("SELECT id FROM kp WHERE record_id='wiki:Ch1/A.md'")[0]['id']
    repo.update_kp(node, {'gist': '用户自己校订的摘要'}, origin='manual-test')
    (env['wiki'] / 'Ch1/A.md').write_text(
        '# 甲概念\n\n## 摘要\nWiki 的新摘要。\n\n## 详细内容\nWiki 的新正文。\n',
        encoding='utf-8')

    result = wiki_import.import_pages(env['wiki'], allow_api=True)

    row = _rows('SELECT gist,detail FROM kp WHERE id=?', (node,))[0]
    assert row['gist'] == '用户自己校订的摘要'
    assert 'Wiki 的新正文。' in row['detail']
    assert result['manual_fields_preserved'] >= 1


def test_import_does_not_merge_manual_names_or_second_vault(env):
    repo.create_course(COURSE, workspace='workpal')
    conn = db.connect()
    with conn:
        conn.execute("INSERT INTO kp(course_id,name,record_id) VALUES(?,?,?)",
                     (_course_id(), '甲概念', 'manual:1'))
    conn.close()
    result = wiki_import.import_pages(env['wiki'], allow_api=True)
    assert result['nodes_created'] == 3
    assert _scalar('SELECT COUNT(*) FROM kp') == 4
    second = env['tmp'] / 'second' / 'wiki'
    _write_pages(second)
    with pytest.raises(ValueError, match='主题'):
        wiki_import.import_pages(second, allow_api=True)
    assert wiki_import.import_pages(second, '另一个主题', allow_api=True)['nodes_created'] == 3


def test_relative_wikilinks_ignore_code_and_report_unresolved(env):
    (env['wiki'] / 'Ch1/A.md').write_text(
        '# A\n[[./B]] [[Missing]]\n```md\n[[C]]\n```\n`[[C]]`', encoding='utf-8')
    result = wiki_import.import_pages(env['wiki'], allow_api=True)
    assert result['edges_created'] == 3
    assert result['unresolved_details'] == [{'page': 'Ch1/A.md', 'target': 'Missing'}]


def test_empty_root_rejected_and_no_source_changes(env):
    empty = env['tmp'] / 'empty'
    empty.mkdir()
    with pytest.raises(ValueError, match='Markdown'):
        wiki_import.import_pages(empty, allow_api=True)
    assert _scalar('SELECT COUNT(*) FROM courses') == 0
    with _api_client() as client:
        assert client.post('/api/wiki/import', json={'vault_root': ''}).status_code == 400
        assert client.post('/api/wiki/import', json={'vault_root': str(env['wiki']), 'course': '..'}).status_code == 400


def test_obsidian_yaml_lists_and_aliases(env):
    (env['wiki'] / 'Ch1/A.md').write_text(
        '---\ntitle: 123\ntags:\n  - 方法\n  - 管理\naliases: [甲别名]\n---\n正文', encoding='utf-8')
    (env['wiki'] / 'Ch1/B.md').write_text('# B\n[[甲别名]]', encoding='utf-8')
    result = wiki_import.import_pages(env['wiki'], allow_api=True)
    assert result['edges_created'] == 2
    row = _rows("SELECT * FROM kp WHERE record_id='wiki:Ch1/A.md'")[0]
    assert row['name'] == '123'
    assert row['band'] == '方法'


def test_legacy_malformed_frontmatter_still_imports_with_warning(env):
    (env['wiki'] / 'Ch1/A.md').write_text(
        '---\ntitle: "AI"：说明\ntype: guide\n---\n# AI\n正文', encoding='utf-8')
    result = wiki_import.import_pages(env['wiki'], allow_api=True)
    assert result['pages'] == 3
    assert result['parse_warnings'] == ['Ch1/A.md']


def test_reimport_preserves_manual_relation_governance(env):
    wiki_import.import_pages(env['wiki'], allow_api=True)
    conn = db.connect()
    with conn:
        conn.execute("UPDATE knowledge_relation_meta SET reason='用户确认',status='rejected'")
    conn.close()
    wiki_import.import_pages(env['wiki'], allow_api=True)
    assert _scalar("SELECT COUNT(*) FROM knowledge_relation_meta WHERE reason='用户确认' AND status='rejected'") == 3


def test_import_refuses_destination_symlink(env):
    course_root = env['data'] / 'WorkPal' / COURSE
    course_root.mkdir(parents=True)
    outside = env['tmp'] / 'outside'
    outside.mkdir()
    (course_root / 'wiki').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='符号链接'):
        wiki_import.import_pages(env['wiki'], allow_api=True)
    assert list(outside.iterdir()) == []


def test_progress_callback_per_page(env):
    seen = []
    wiki_import.import_pages(env['wiki'], allow_api=True,
                             progress=lambda processed, total: seen.append((processed, total)))
    assert seen == [(1, 3), (2, 3), (3, 3)]


def test_dry_run_only_inventories(env):
    seen = []
    result = wiki_import.import_pages(
        env['wiki'], apply=False,
        progress=lambda processed, total: seen.append((processed, total)))
    assert result['dry_run'] is True
    assert result['pages'] == 3
    assert result['unique_titles'] == 3
    assert result['links'] == 4  # A:2 + B:1 + C:1
    assert seen == [(1, 3), (2, 3), (3, 3)]
    assert _scalar('SELECT COUNT(*) FROM courses') == 0
    assert _scalar('SELECT COUNT(*) FROM kp') == 0


def test_wiki_uid_move_preserves_node_source_document_and_mastery(env):
    page = env['wiki'] / 'Ch1/A.md'
    page.write_text(PAGE_A.replace('title:', 'uid: stable-a\ntitle:', 1), encoding='utf-8')
    wiki_import.import_pages(env['wiki'], allow_api=True)
    node = _rows("SELECT * FROM kp WHERE record_id='wiki:Ch1/A.md'")[0]
    source = _rows("SELECT * FROM source_files WHERE path LIKE '%/Ch1/A.md'")[0]
    document = _rows('SELECT * FROM knowledge_documents WHERE kp_id=?', (node['id'],))[0]
    repo.update_kp(node['id'], {'gist': '我的摘要'}, origin='manual-test')
    conn = db.connect()
    with conn:
        conn.execute("UPDATE kp SET mastery='会用' WHERE id=?", (node['id'],))
    conn.close()
    moved = env['wiki'] / 'Ch2/Renamed.md'
    moved.parent.mkdir()
    page.rename(moved)

    preview = wiki_import.import_pages(env['wiki'], apply=False)
    assert preview['change_counts']['moved'] == 1
    assert preview['change_counts']['missing'] == 0
    assert preview['can_apply'] is True
    assert _rows('SELECT path FROM source_files WHERE id=?', (source['id'],))[0]['path'] == source['path']
    result = wiki_import.import_pages(env['wiki'], allow_api=True)
    after = _rows('SELECT * FROM kp WHERE id=?', (node['id'],))[0]
    assert result['nodes_created'] == result['sources_created'] == 0
    assert after['record_id'] == 'wiki:Ch2/Renamed.md'
    assert after['gist'] == '我的摘要' and after['mastery'] == '会用'
    assert _rows('SELECT id FROM knowledge_documents WHERE kp_id=?', (node['id'],))[0]['id'] == document['id']
    assert _rows('SELECT path FROM source_files WHERE id=?', (source['id'],))[0]['path'].endswith('/Ch2/Renamed.md')
    assert _scalar('SELECT COUNT(*) FROM source_snapshots WHERE source_id=?', (source['id'],)) == 1
    attrs = json.loads(_rows('SELECT attributes_json FROM knowledge_node_meta WHERE kp_id=?', (node['id'],))[0]['attributes_json'])
    assert attrs['wiki_uid'] == 'stable-a'
    assert 'Ch1/A.md' in attrs['wiki_path_history']
    again = wiki_import.import_pages(env['wiki'], apply=False)
    assert again['change_counts']['unchanged'] == 3


def test_wiki_preview_reports_missing_without_deleting_knowledge(env):
    wiki_import.import_pages(env['wiki'], allow_api=True)
    before = _scalar('SELECT COUNT(*) FROM kp')
    (env['wiki'] / 'Ch1/A.md').unlink()
    preview = wiki_import.import_pages(env['wiki'], apply=False)
    assert preview['change_counts']['missing'] == 1
    missing = next(row for row in preview['changes'] if row['kind'] == 'missing')
    assert missing['relative'] == 'Ch1/A.md'
    assert missing['requires_review'] is True
    result = wiki_import.import_pages(env['wiki'], allow_api=True)
    assert result['change_counts']['missing'] == 1
    assert _scalar('SELECT COUNT(*) FROM kp') == before
    assert _scalar("SELECT COUNT(*) FROM source_files WHERE path LIKE '%/Ch1/A.md'") == 1


def test_wiki_duplicate_uids_block_entire_import_before_copy(env):
    for name in ('A', 'B'):
        page = env['wiki'] / f'Ch1/{name}.md'
        page.write_text(page.read_text().replace('title:', 'id: duplicate\ntitle:', 1), encoding='utf-8')
    preview = wiki_import.import_pages(env['wiki'], apply=False)
    assert preview['can_apply'] is False
    assert any(item['kind'] == 'duplicate_identity' for item in preview['conflicts'])
    with pytest.raises(ValueError, match='身份'):
        wiki_import.import_pages(env['wiki'], allow_api=True)
    assert _scalar('SELECT COUNT(*) FROM kp') == 0
    assert not (env['data'] / 'WorkPal' / COURSE / 'wiki').exists()


def test_wiki_uid_can_be_adopted_at_same_path_but_not_replaced(env):
    wiki_import.import_pages(env['wiki'], allow_api=True)
    page = env['wiki'] / 'Ch1/A.md'
    page.write_text(PAGE_A.replace('title:', 'uid: first\ntitle:', 1), encoding='utf-8')
    assert wiki_import.import_pages(env['wiki'], allow_api=True)['nodes_created'] == 0
    page.write_text(PAGE_A.replace('title:', 'uid: second\ntitle:', 1), encoding='utf-8')
    preview = wiki_import.import_pages(env['wiki'], apply=False)
    assert preview['can_apply'] is False
    with pytest.raises(ValueError, match='身份'):
        wiki_import.import_pages(env['wiki'], allow_api=True)


def test_wiki_without_uid_does_not_guess_moves_from_titles(env):
    wiki_import.import_pages(env['wiki'], allow_api=True)
    (env['wiki'] / 'Ch1/A.md').rename(env['wiki'] / 'Ch1/Renamed.md')
    preview = wiki_import.import_pages(env['wiki'], apply=False)
    assert preview['change_counts']['added'] == 1
    assert preview['change_counts']['missing'] == 1
    assert preview['change_counts']['moved'] == 0


def test_wiki_preview_does_not_create_a_database(env, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', env['tmp'] / 'absent' / 'memory.db')
    result = wiki_import.import_pages(env['wiki'], apply=False)
    assert result['change_counts']['added'] == 3
    assert not db.DB_PATH.parent.exists()


def test_wiki_preview_api_is_read_only_and_reports_conflicts(env):
    page = env['wiki'] / 'Ch1/A.md'
    page.write_text(PAGE_A.replace('title:', 'uid: stable\ntitle:', 1), encoding='utf-8')
    (env['wiki'] / 'duplicate.md').write_text(page.read_text(), encoding='utf-8')
    with _api_client() as client:
        response = client.post('/api/wiki/import/preview', json={'vault_root': str(env['wiki'])})
    assert response.status_code == 200
    assert response.json()['can_apply'] is False
    assert _scalar('SELECT COUNT(*) FROM kp') == 0


def test_wiki_preview_api_rejects_empty_directory(env):
    with _api_client() as client:
        response = client.post('/api/wiki/import/preview', json={'vault_root': ''})
    assert response.status_code == 400


@pytest.mark.parametrize('identity', ['uid: [a, b]', 'uid: a\nid: b', 'uid: true'])
def test_wiki_invalid_identity_is_not_silently_ignored(env, identity):
    (env['wiki'] / 'Ch1/A.md').write_text(
        f'---\n{identity}\ntitle: Alpha\n---\nA', encoding='utf-8')
    with pytest.raises(ValueError, match='身份'):
        wiki_import.import_pages(env['wiki'], allow_api=True)
    assert _scalar('SELECT COUNT(*) FROM kp') == 0


def test_wiki_move_into_existing_source_cannot_overwrite_file(env):
    page = env['wiki'] / 'Ch1/A.md'
    page.write_text(PAGE_A.replace('title:', 'uid: stable\ntitle:', 1), encoding='utf-8')
    wiki_import.import_pages(env['wiki'], allow_api=True)
    destination = env['data'] / 'WorkPal' / COURSE / 'wiki' / 'Other.md'
    destination.write_text('Protected unrelated source', encoding='utf-8')
    conn = db.connect()
    with conn:
        conn.execute('INSERT INTO source_files(course_id,path) VALUES(?,?)', (_course_id(), str(destination)))
    conn.close()
    page.rename(env['wiki'] / 'Other.md')
    with pytest.raises(ValueError):
        wiki_import.import_pages(env['wiki'], allow_api=True)
    assert destination.read_text() == 'Protected unrelated source'


def test_wiki_empty_vault_previews_missing_and_restore_clears_it(env):
    wiki_import.import_pages(env['wiki'], allow_api=True)
    for page in env['wiki'].rglob('*.md'):
        page.unlink()
    assert wiki_import.import_pages(env['wiki'], apply=False)['change_counts']['missing'] == 3
    _write_pages(env['wiki'])
    result = wiki_import.import_pages(env['wiki'], allow_api=True)
    assert result['change_counts']['missing'] == 0
    assert result['nodes_created'] == 0


def test_wiki_added_page_cannot_claim_unrelated_source_without_file(env):
    wiki_import.import_pages(env['wiki'], allow_api=True)
    path = env['data'] / 'WorkPal' / COURSE / 'wiki' / 'New.md'
    conn = db.connect()
    with conn:
        source_id = conn.execute('INSERT INTO source_files(course_id,path) VALUES(?,?)',
                                 (_course_id(), str(path))).lastrowid
        conn.execute("INSERT INTO evidence(source_id,content) VALUES(?,'unrelated evidence')", (source_id,))
    conn.close()
    (env['wiki'] / 'New.md').write_text('# New', encoding='utf-8')
    assert wiki_import.import_pages(env['wiki'], apply=False)['can_apply'] is False
    with pytest.raises(ValueError):
        wiki_import.import_pages(env['wiki'], allow_api=True)
    assert _rows('SELECT stale FROM evidence WHERE source_id=?', (source_id,))[0]['stale'] == 0
    assert not path.exists()


def test_wiki_removing_established_uid_requires_review(env):
    page = env['wiki'] / 'Ch1/A.md'
    page.write_text(PAGE_A.replace('title:', 'uid: alpha\ntitle:', 1), encoding='utf-8')
    wiki_import.import_pages(env['wiki'], allow_api=True)
    page.write_text(PAGE_A, encoding='utf-8')
    assert wiki_import.import_pages(env['wiki'], apply=False)['can_apply'] is False
    with pytest.raises(ValueError):
        wiki_import.import_pages(env['wiki'], allow_api=True)


def test_wiki_reimport_preserves_divergent_local_copy(env):
    wiki_import.import_pages(env['wiki'], allow_api=True)
    path = env['data'] / 'WorkPal' / COURSE / 'wiki' / 'Ch1/A.md'
    path.write_text('User edited local copy', encoding='utf-8')
    preview = wiki_import.import_pages(env['wiki'], apply=False)
    assert preview['can_apply'] is False
    assert any(row['kind'] == 'local_copy_changed' for row in preview['conflicts'])
    with pytest.raises(ValueError):
        wiki_import.import_pages(env['wiki'], allow_api=True)
    assert path.read_text() == 'User edited local copy'


def test_import_validates_root_and_course(env):
    with pytest.raises(FileNotFoundError):
        wiki_import.import_pages(env['tmp'] / 'missing', allow_api=True)
    for course in ('', 'a' * 81, 'x/y', 'x' + chr(92) + 'y'):
        with pytest.raises(ValueError):
            wiki_import.import_pages(env['wiki'], course, allow_api=True)
    assert _scalar('SELECT COUNT(*) FROM courses') == 0


# --- 检索可见性 -------------------------------------------------------------


def test_wiki_pages_searchable_and_development_doc_still_hidden(env):
    wiki_import.import_pages(env['wiki'], allow_api=True)
    course_id = _course_id()
    conn = db.connect()
    try:
        with conn:
            dev_source = conn.execute(
                """INSERT INTO source_files(course_id,path,sha256,status,source_kind,source_role)
                   VALUES(?,?,?,'indexed','development_doc','platform_document')""",
                (course_id, '/tmp/dev-wiki.md', 'devhash')).lastrowid
            conn.execute('INSERT INTO source_chunks(source_id,position,content) VALUES(?,?,?)',
                         (dev_source, 0, DEV_QUERY + ' 开发内部说明'))
            conn.execute(
                """INSERT INTO evidence(source_id,locator_type,locator_start,locator_end,content,content_hash)
                   VALUES(?,?,?,?,?,?)""",
                (dev_source, 'line', 1, 1, DEV_QUERY + ' 开发内部说明', 'devhash-1'))
    finally:
        conn.close()

    hits = repo.memory_search_hits(WIKI_QUERY, workspace='workpal')
    assert hits, 'wiki_page 内容应能被默认检索命中'
    assert any('A.md' in str(hit.get('source') or '') for hit in hits)
    assert all('/tmp/dev-wiki.md' != hit.get('source') for hit in hits)

    assert repo.memory_search_hits(DEV_QUERY, workspace='workpal') == []
    dev_hits = repo.memory_search_hits(DEV_QUERY, workspace='workpal', include_dev=True)
    assert dev_hits
    assert any('/tmp/dev-wiki.md' in str(hit.get('source') or '') for hit in dev_hits)


def test_legacy_development_doc_rows_upgrade_on_reimport(env):
    wiki_import.import_pages(env['wiki'], allow_api=True)
    course_id = _course_id()
    conn = db.connect()
    try:
        with conn:
            conn.execute("UPDATE source_files SET source_kind='development_doc',"
                         " source_role='platform_document', catalog_status='none'"
                         ' WHERE course_id=?', (course_id,))
    finally:
        conn.close()
    assert repo.memory_search_hits(WIKI_QUERY, workspace='workpal') == []

    result = wiki_import.import_pages(env['wiki'], allow_api=True)
    assert result['sources_updated'] == 3
    assert result['nodes_updated'] == 3
    rows = _rows('SELECT source_kind, source_role FROM source_files')
    assert {row['source_kind'] for row in rows} == {'wiki_page'}
    assert {row['source_role'] for row in rows} == {'wiki_page'}
    assert repo.memory_search_hits(WIKI_QUERY, workspace='workpal')


def test_source_metadata_contract_for_wiki_page():
    assert 'wiki_page' in sources.SOURCE_KINDS
    assert sources.source_metadata('wiki_page') == {
        'source_kind': 'wiki_page', 'source_role': 'wiki_page', 'catalog_status': 'none'}
    assert sources.normalize_source_kind('wiki_page') == 'wiki_page'
    assert sources.normalize_source_kind('bogus') == 'unclassified'
    assert sources.source_metadata('development_doc') == {
        'source_kind': 'development_doc', 'source_role': 'platform_document',
        'catalog_status': 'none'}


# --- Obsidian 仓库发现 ------------------------------------------------------


def test_vault_candidates_prefers_wiki_child(env):
    plain = env['tmp'] / 'plainvault'
    plain.mkdir()
    missing = env['tmp'] / 'gone'
    config = env['tmp'] / 'obsidian-multi.json'
    config.write_text(json.dumps({'vaults': {
        'one': {'path': str(env['vault'])},
        'two': {'path': str(plain)},
        'three': {'path': str(missing)},
    }}), encoding='utf-8')
    candidates = wiki_import.vault_candidates([config])
    assert [item['path'] for item in candidates] == sorted(item['path'] for item in candidates)
    by_path = {item['path']: item for item in candidates}
    wiki_entry = by_path[str(env['wiki'].resolve())]
    assert wiki_entry['has_wiki'] is True
    assert wiki_entry['label'] == 'vault'
    plain_entry = by_path[str(plain.resolve())]
    assert plain_entry['has_wiki'] is False
    assert len(candidates) == 2

    broken = env['tmp'] / 'broken.json'
    broken.write_text('{not json', encoding='utf-8')
    assert wiki_import.vault_candidates([broken, env['tmp'] / 'none.json']) == []


# --- 状态机 -----------------------------------------------------------------


def test_status_lifecycle_done(env):
    idle = wiki_import.status()
    for key in ('state', 'started_at', 'finished_at', 'wiki_root', 'course',
                'total', 'done', 'result', 'error'):
        assert key in idle
    assert idle['state'] == 'idle'
    assert idle['result'] is None

    started = wiki_import.start_import(env['wiki'])
    assert started['state'] in {'running', 'done'}
    assert started['wiki_root'] == str(env['wiki'].resolve())
    assert started['course'] == COURSE

    state = _wait_done()
    assert state['state'] == 'done'
    assert state['error'] == ''
    assert state['total'] == 3
    assert state['done'] == 3
    assert state['result']['pages'] == 3
    assert state['result']['workspace'] == 'workpal'
    assert state['finished_at']

    with pytest.raises(FileNotFoundError):
        wiki_import.start_import(env['tmp'] / 'missing')
    with pytest.raises(ValueError):
        wiki_import.start_import(env['wiki'], 'x/y')


def test_concurrent_start_keeps_single_job(env, monkeypatch):
    gate = threading.Event()
    calls = []

    def fake_import(root, course, **kwargs):
        calls.append((Path(root), course, kwargs))
        gate.wait(5)
        return {'pages': 3, 'course': course, 'workspace': 'workpal'}

    monkeypatch.setattr(wiki_import, 'import_pages', fake_import)
    first = wiki_import.start_import(env['wiki'])
    assert first['state'] == 'running'
    second = wiki_import.start_import(env['wiki'])
    assert second['state'] == 'running'
    assert second['started_at'] == first['started_at']
    assert len(calls) == 1
    gate.set()
    state = _wait_done()
    assert state['state'] == 'done'
    assert state['total'] == 3 and state['done'] == 3


def test_worker_exception_lands_in_error_state(env, monkeypatch):
    def boom(root, course, **kwargs):
        raise RuntimeError('模拟导入失败')

    monkeypatch.setattr(wiki_import, 'import_pages', boom)
    wiki_import.start_import(env['wiki'])
    state = _wait_done()
    assert state['state'] == 'error'
    assert '模拟导入失败' in state['error']
    assert state['result'] is None
    assert state['finished_at']


# --- API --------------------------------------------------------------------


def test_api_status_and_start(env, monkeypatch):
    monkeypatch.setattr(wiki_import, 'vault_candidates', lambda config_paths=None: [
        {'path': str(env['wiki']), 'label': 'vault', 'has_wiki': True}])
    with _api_client() as client:
        status = client.get('/api/wiki/import/status')
        assert status.status_code == 200
        body = status.json()
        assert body['state'] == 'idle'
        assert body['candidates'] == [{'path': str(env['wiki']), 'label': 'vault',
                                       'has_wiki': True}]

        started = client.post('/api/wiki/import', json={'vault_root': str(env['wiki'])})
        assert started.status_code == 202
        payload = started.json()
        assert payload['ok'] is True
        assert payload['state'] in {'running', 'done'}
        assert payload['course'] == COURSE

        deadline = time.time() + 15
        while time.time() < deadline:
            body = client.get('/api/wiki/import/status').json()
            if body['state'] in {'done', 'error'}:
                break
            time.sleep(0.02)
        assert body['state'] == 'done'
        assert body['result']['pages'] == 3


def test_api_validation_and_busy(env, monkeypatch):
    with _api_client() as client:
        assert client.post('/api/wiki/import', json={
            'vault_root': str(env['tmp'] / 'missing')}).status_code == 400
        for course in ('x/y', 'a' * 81):
            assert client.post('/api/wiki/import', json={
                'vault_root': str(env['wiki']), 'course': course}).status_code == 400

    gate = threading.Event()

    def slow_import(root, course, **kwargs):
        gate.wait(5)
        return {'pages': 0, 'course': course, 'workspace': 'workpal'}

    monkeypatch.setattr(wiki_import, 'import_pages', slow_import)
    with _api_client() as client:
        first = client.post('/api/wiki/import', json={'vault_root': str(env['wiki'])})
        assert first.status_code == 202
        busy = client.post('/api/wiki/import', json={'vault_root': str(env['wiki'])})
        assert busy.status_code == 409
        gate.set()
        deadline = time.time() + 10
        while time.time() < deadline and wiki_import.status()['state'] == 'running':
            time.sleep(0.02)
        assert wiki_import.status()['state'] == 'done'


def test_wiki_sync_without_fts_keeps_history(env):
    conn = db.connect()
    try:
        with conn:
            triggers = conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'search_fts_%'").fetchall()
            for trigger in triggers:
                conn.execute('DROP TRIGGER "' + trigger['name'] + '"')
            conn.execute('DROP TABLE search_fts')
    finally:
        conn.close()
    wiki_import.import_pages(env['wiki'], allow_api=True)
    (env['wiki'] / 'Ch1/A.md').write_text('# A\n新的正文', encoding='utf-8')
    wiki_import.import_pages(env['wiki'], allow_api=True)
    assert _scalar('SELECT COUNT(*) FROM evidence WHERE stale=1') > 0


def test_wiki_unchanged_and_changed_pages_preserve_manual_name(env):
    wiki_import.import_pages(env['wiki'], allow_api=True)
    node = _rows("SELECT id FROM kp WHERE record_id='wiki:Ch1/A.md'")[0]['id']
    repo.update_kp(node, {'name': '用户标题'}, origin='manual-test')
    (env['wiki'] / 'Ch1/B.md').write_text('# 用户标题\n另一个页面', encoding='utf-8')
    wiki_import.import_pages(env['wiki'], allow_api=True)
    assert _scalar('SELECT name FROM kp WHERE id=?', (node,)) == '用户标题'
    assert _scalar('SELECT COUNT(*) FROM kp') == 3


def test_wiki_import_owned_document_tracks_source_versions(env):
    wiki_import.import_pages(env['wiki'], allow_api=True)
    node = _rows("SELECT id FROM kp WHERE record_id='wiki:Ch1/A.md'")[0]['id']
    (env['wiki'] / 'Ch1/A.md').write_text('# A\n新版本正文', encoding='utf-8')
    wiki_import.import_pages(env['wiki'], allow_api=True)
    document = _rows('SELECT * FROM knowledge_documents WHERE kp_id=?', (node,))[0]
    assert document['version'] == 2
    assert '新版本正文' in document['body_md']
    assert _scalar('SELECT COUNT(*) FROM document_revisions WHERE document_id=?', (document['id'],)) == 2
    wiki_import.import_pages(env['wiki'], allow_api=True)
    assert _scalar('SELECT version FROM knowledge_documents WHERE id=?', (document['id'],)) == 2


def test_wiki_title_case_collision_is_disambiguated(env):
    wiki_import.import_pages(env['wiki'], allow_api=True)
    nodes = {row['record_id']: row['id'] for row in _rows('SELECT id,record_id FROM kp')}
    a, b = nodes['wiki:Ch1/A.md'], nodes['wiki:Ch1/B.md']
    repo.update_kp(b, {'name': 'Existing'}, origin='manual-test')
    repo.ensure_node_identity(b)
    repo.ensure_node_identity(a)
    (env['wiki'] / 'Ch1/A.md').write_text('# existing\n新正文', encoding='utf-8')
    wiki_import.import_pages(env['wiki'], allow_api=True)
    rows = _rows('SELECT id,name FROM kp')
    names = [' '.join(row['name'].split()).casefold() for row in rows]
    assert len(set(names)) == 3
    assert _scalar('SELECT name FROM kp WHERE id=?', (b,)) == 'Existing'
