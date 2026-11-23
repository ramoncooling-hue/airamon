"""Assisted extraction must respect workspace, source, and review boundaries."""

import json
from contextlib import closing
from unittest.mock import patch

import pytest

from app.memory import db, domain_profiles, sources


def model_response(*, refs=(2,), gist='Generated explanation', chapter='Model chapter'):
    return json.dumps({
        'summary': 'Source summary',
        'concepts': [{
            'name': 'Boundary concept', 'kp_type': 'concept', 'chapter': chapter,
            'gist': gist, 'detail': 'Generated detail', 'trap': '',
            'evidence_refs': list(refs),
        }],
    })


@pytest.fixture
def indexed_source(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'APP_DIR', tmp_path / 'app')
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'app' / 'test.db')
    db.apply_schema()

    def create(*, key='workpal', kind='work', domain='', course='MBA plans', text=None):
        with closing(db.connect()) as conn, conn:
            workspace_id = None
            if key is not None:
                conn.execute('''INSERT INTO workspaces(key,name,kind,domain) VALUES(?,?,?,?)
                                ON CONFLICT(key) DO UPDATE SET kind=excluded.kind,domain=excluded.domain''',
                             (key, key, kind, domain))
                workspace_id = conn.execute('SELECT id FROM workspaces WHERE key=?', (key,)).fetchone()[0]
            conn.execute('INSERT INTO courses(name,workspace_id) VALUES(?,?)', (course, workspace_id))
        course_dir = tmp_path / 'course'
        inbox = course_dir / 'inbox'
        inbox.mkdir(parents=True)
        path = inbox / 'lesson.md'
        path.write_text(text if text is not None else '# Heading\nFirst evidence.\nSecond evidence.\n',
                        encoding='utf-8')
        scanned = sources.scan_course(course, [inbox])
        assert scanned['indexed'] == 1, scanned
        with closing(db.connect()) as conn:
            source_id = conn.execute('SELECT id FROM source_files WHERE path=?', (str(path),)).fetchone()[0]
        return course, path, course_dir, source_id

    return create


def artifact_state(course_dir):
    return {str(path.relative_to(course_dir)): path.read_bytes()
            for path in course_dir.rglob('*') if path.is_file()}


def review_state():
    with closing(db.connect()) as conn:
        return {table: [tuple(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY 1')]
                for table in ('knowledge_candidates', 'knowledge_candidate_evidence',
                              'candidate_reviews', 'knowledge_notes')}


@pytest.mark.parametrize('key,kind,domain,expected', [
    ('workpal', 'work', '', 'work'),
    ('team-x', 'work', '', 'work'),
    ('lifepal', 'life', '', 'life'),
    ('private', 'custom', '', 'general'),
    ('workpal', 'custom', '', 'general'),
    ('private', 'unknown', '', 'general'),
    ('private', '', '', 'general'),
    ('private', 'custom', 'mba', 'general'),
    ('private', 'custom', 'work', 'general'),
    ('team-x', 'work', 'mba', 'work'),
    ('team-x', 'work', 'life', 'work'),
    ('team-x', 'work', 'missing-profile', 'work'),
    ('lifepal', 'life', 'mba', 'life'),
    ('study-x', 'study', 'work', 'mba'),
    (None, None, '', 'mba'),
])
def test_actual_workspace_kind_bounds_domain_despite_mba_title(indexed_source, key, kind, domain, expected):
    course, path, _, _ = indexed_source(key=key, kind=kind, domain=domain)
    with patch.object(sources.wiki.llm, 'chat', return_value=model_response()) as model:
        result = sources.assist_wiki(course, str(path))
    payload = json.loads(model.call_args.args[1][1]['content'])
    assert payload['domain_contract']['id'] == expected
    with open(result['dictionary'], encoding='utf-8') as stream:
        assert json.load(stream)['domain'] == expected
    if expected != 'mba':
        assert 'exam_point' not in payload['domain_contract']['node_types']


def test_legacy_course_without_workspace_defaults_to_study(indexed_source):
    course, path, _, _ = indexed_source(key=None, course='Work methods')
    with patch.object(sources.wiki.llm, 'chat', return_value=model_response()) as model:
        sources.assist_wiki(course, str(path))
    assert json.loads(model.call_args.args[1][1]['content'])['domain_contract']['id'] == 'mba'


def test_compatible_explicit_domain_profile_is_used(indexed_source, monkeypatch):
    profiles = dict(domain_profiles.load_profiles())
    profiles['industry'] = dict(profiles['work'], id='industry', course_patterns=[], workspace_default=False)
    monkeypatch.setattr(domain_profiles, 'load_profiles', lambda: profiles)
    course, path, _, _ = indexed_source(key='team-x', domain='industry')
    with patch.object(sources.wiki.llm, 'chat', return_value=model_response()) as model:
        sources.assist_wiki(course, str(path))
    assert json.loads(model.call_args.args[1][1]['content'])['domain_contract']['id'] == 'industry'


@pytest.mark.parametrize('change', ['content', 'whitespace'])
def test_changed_source_fails_before_model_or_artifact_writes(indexed_source, change):
    course, path, course_dir, _ = indexed_source()
    text = path.read_text(encoding='utf-8')
    path.write_text(text.replace('First evidence.', 'Other evidence.') if change == 'content' else text + '\n',
                    encoding='utf-8')
    before_artifacts, before_reviews = artifact_state(course_dir), review_state()
    with patch.object(sources.wiki, 'assist_draft', wraps=sources.wiki.assist_draft) as draft, \
            patch.object(sources.wiki.llm, 'chat', return_value=model_response()) as model:
        with pytest.raises(ValueError, match='重新扫描'):
            sources.assist_wiki(course, str(path))
    draft.assert_not_called()
    model.assert_not_called()
    assert artifact_state(course_dir) == before_artifacts
    assert review_state() == before_reviews


@pytest.mark.parametrize('field,value', [
    ('content', 'Unmatched text'), ('locator_type', 'paragraph'),
    ('locator_start', 99), ('locator_end', 99), ('stale', 1),
])
def test_same_count_unmatched_evidence_fails_before_writes(indexed_source, field, value):
    course, path, course_dir, source_id = indexed_source()
    with closing(db.connect()) as conn, conn:
        conn.execute(f'UPDATE evidence SET {field}=? WHERE source_id=? AND locator_start=2',
                     (value, source_id))
        if field == 'stale':
            conn.execute('''INSERT INTO evidence
                            (source_id,locator_type,locator_start,locator_end,content,content_hash)
                            VALUES(?,'line',99,99,'Unrelated evidence','decoy')''', (source_id,))
        assert conn.execute('SELECT COUNT(*) FROM evidence WHERE source_id=? AND stale=0',
                            (source_id,)).fetchone()[0] == 3
    before_artifacts, before_reviews = artifact_state(course_dir), review_state()
    with patch.object(sources.wiki, 'assist_draft', wraps=sources.wiki.assist_draft) as draft, \
            patch.object(sources.wiki.llm, 'chat', return_value=model_response()) as model:
        with pytest.raises(ValueError, match='重新扫描'):
            sources.assist_wiki(course, str(path))
    draft.assert_not_called()
    model.assert_not_called()
    assert artifact_state(course_dir) == before_artifacts
    assert review_state() == before_reviews


def test_file_change_during_extraction_fails_before_writes(indexed_source):
    course, path, course_dir, _ = indexed_source()
    extract = sources.extract_structured

    def changed_extract(document):
        result = extract(document)
        document.write_text(document.read_text(encoding='utf-8') + '\n', encoding='utf-8')
        return result

    before_reviews = review_state()
    before_artifacts = {key: value for key, value in artifact_state(course_dir).items()
                        if key != 'inbox/lesson.md'}
    with patch.object(sources, 'extract_structured', side_effect=changed_extract), \
            patch.object(sources.wiki, 'assist_draft', wraps=sources.wiki.assist_draft) as draft, \
            patch.object(sources.wiki.llm, 'chat', return_value=model_response()) as model:
        with pytest.raises(ValueError, match='重新扫描'):
            sources.assist_wiki(course, str(path))
    draft.assert_not_called()
    model.assert_not_called()
    assert {key: value for key, value in artifact_state(course_dir).items()
            if key != 'inbox/lesson.md'} == before_artifacts
    assert review_state() == before_reviews


@pytest.mark.parametrize('status,action', [
    ('confirmed', 'confirmed'), ('rejected', 'rejected'),
    ('merged', 'merged'), ('duplicate', 'duplicate'),
    ('pending', 'edited'), ('superseded', 'edited'),
])
def test_reviewed_candidate_text_and_citations_survive_rerun(indexed_source, status, action):
    course, path, _, _ = indexed_source()
    with patch.object(sources.wiki.llm, 'chat', return_value=model_response(refs=(2,))):
        sources.assist_wiki(course, str(path))
    with closing(db.connect()) as conn, conn:
        candidate_id = conn.execute("SELECT id FROM knowledge_candidates WHERE name='Boundary concept'").fetchone()[0]
        conn.execute('''UPDATE knowledge_candidates SET status=?,gist='Human explanation',detail='Human detail',
                        trap='Human caveat' WHERE id=?''', (status, candidate_id))
        conn.execute('INSERT INTO candidate_reviews(candidate_id,action,note) VALUES(?,?,?)',
                     (candidate_id, action, 'Reviewed manually'))
    before = review_state()
    with patch.object(sources.wiki.llm, 'chat', return_value=model_response(refs=(3,), gist='Replacement')):
        sources.assist_wiki(course, str(path))
    after = review_state()
    for table in ('knowledge_candidates', 'knowledge_candidate_evidence', 'candidate_reviews'):
        assert after[table] == before[table]


@pytest.mark.parametrize('status,action', [('pending', None), ('superseded', 'superseded')])
def test_unreviewed_candidate_can_refresh_with_exact_citations(indexed_source, status, action):
    course, path, _, source_id = indexed_source()
    with patch.object(sources.wiki.llm, 'chat', return_value=model_response(refs=(2,))):
        sources.assist_wiki(course, str(path))
    with closing(db.connect()) as conn, conn:
        candidate_id = conn.execute("SELECT id FROM knowledge_candidates WHERE name='Boundary concept'").fetchone()[0]
        conn.execute('UPDATE knowledge_candidates SET status=?,superseded_by=? WHERE id=?',
                     (status, candidate_id if action else None, candidate_id))
        if action:
            conn.execute('INSERT INTO candidate_reviews(candidate_id,action) VALUES(?,?)', (candidate_id, action))
    with patch.object(sources.wiki.llm, 'chat', return_value=model_response(refs=(3,), gist='Replacement')):
        sources.assist_wiki(course, str(path))
    with closing(db.connect()) as conn:
        candidate = conn.execute('SELECT status,gist,superseded_by FROM knowledge_candidates WHERE id=?',
                                 (candidate_id,)).fetchone()
        assert tuple(candidate) == ('pending', 'Replacement', None)
        links = conn.execute('''SELECT e.source_id,e.locator_type,e.locator_start,e.locator_end,e.content
                                FROM knowledge_candidate_evidence kce JOIN evidence e ON e.id=kce.evidence_id
                                WHERE kce.candidate_id=?''', (candidate_id,)).fetchall()
        assert [tuple(row) for row in links] == [(source_id, 'line', 3, 3, 'Second evidence.')]


def test_chapter_subset_and_empty_evidence_keep_exact_reference_mapping(indexed_source):
    chapter_one = '\u7b2c1\u7ae0 Introduction \u5b66\u4e60\u76ee\u7684'
    chapter_two = '\u7b2c2\u7ae0 Details \u5b66\u4e60\u76ee\u7684'
    text = f'{chapter_one}\nFirst chapter evidence.\n{chapter_two}\nDetail A.\nDetail B.\n'
    course, path, _, _ = indexed_source(key=None, text=text)
    content, headings, evidence = sources.extract_structured(path)
    evidence.insert(3, {'locator_type': 'page', 'locator_start': 99, 'locator_end': 99, 'text': ''})
    with patch.object(sources, 'extract_structured', return_value=(content, headings, evidence)), \
            patch.object(sources.wiki.llm, 'chat', return_value=model_response(refs=(2, 3))) as model:
        result = sources.assist_wiki(course, str(path), chapter='\u7b2c2\u7ae0')
    payload = json.loads(model.call_args.args[1][1]['content'])
    assert 'First chapter evidence.' not in payload['evidence']
    assert result['concepts'][0]['evidence_refs'] == [3, 4]
    with closing(db.connect()) as conn:
        candidate = conn.execute("SELECT id,chapter FROM knowledge_candidates WHERE name='Boundary concept'").fetchone()
        assert candidate['chapter'] == '\u7b2c2\u7ae0 Details'
        links = conn.execute('''SELECT e.locator_start,e.content FROM knowledge_candidate_evidence kce
                                JOIN evidence e ON e.id=kce.evidence_id
                                WHERE kce.candidate_id=? ORDER BY e.locator_start''', (candidate['id'],)).fetchall()
        assert [tuple(row) for row in links] == [(4, 'Detail A.'), (5, 'Detail B.')]
