#!/usr/bin/env python3
"""Read-only inventory plus isolated incremental-graph characterization.

Private inputs are never emitted in reports. A subprocess receives only a
temporary data root; production database paths are never passed to writers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def readonly_snapshot(database, target):
    source = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        with sqlite3.connect(target) as copied:
            source.backup(copied)
    finally:
        source.close()


def inventory(conn):
    queries = {
        'nodes_by_workspace': '''SELECT w.key,count(*) FROM kp k JOIN courses c ON c.id=k.course_id
            JOIN workspaces w ON w.id=c.workspace_id WHERE k.deleted_at='' GROUP BY w.key''',
        'sources_by_workspace_kind': '''SELECT w.key,s.source_kind,count(*) FROM source_files s
            JOIN courses c ON c.id=s.course_id JOIN workspaces w ON w.id=c.workspace_id GROUP BY 1,2''',
        'relations_by_kind': 'SELECT kind,count(*) FROM kp_edges GROUP BY kind',
        'relations_by_status': 'SELECT status,count(*) FROM knowledge_relation_meta GROUP BY status',
        'cross_workspace_edges': '''SELECT count(*) FROM kp_edges e
            JOIN kp a ON a.id=e.from_kp JOIN kp b ON b.id=e.to_kp
            JOIN courses ca ON ca.id=a.course_id JOIN courses cb ON cb.id=b.course_id
            WHERE ca.workspace_id<>cb.workspace_id''',
        'integrity': 'PRAGMA integrity_check',
        'foreign_key_errors': 'PRAGMA foreign_key_check',
    }
    return {key: [list(row) for row in conn.execute(sql)] for key, sql in queries.items()}


def worker(root):
    sys.path.insert(0, str(ROOT))
    from app.memory import db, documents, repo, sources, wiki_import

    assert db.DATA_DIR.resolve() == (root / 'runtime').resolve()
    db.apply_schema()

    def rows(sql, args=()):
        conn = db.connect()
        try:
            return [dict(row) for row in conn.execute(sql, args)]
        finally:
            conn.close()

    def scalar(sql, args=()):
        return next(iter(rows(sql, args)[0].values()))

    def counts():
        return {table: scalar('SELECT count(*) FROM ' + table) for table in
                ('kp', 'kp_edges', 'evidence', 'source_chunks', 'source_files')}

    result = {}
    wiki = root / 'wiki-sample'
    first = wiki_import.import_pages(wiki, 'Baseline Wiki', allow_api=True)
    before = counts()
    second = wiki_import.import_pages(wiki, 'Baseline Wiki', allow_api=True)
    result['real_wiki_sample'] = {
        'pages': first['pages'], 'edges': first['edges_created'],
        'unresolved_in_sample': first['unresolved_links'],
        'repeat_counts_stable': before == counts(),
        'repeat_nodes_created': second['nodes_created'],
        'repeat_edges_created': second['edges_created'],
        'note': 'Bounded sample; links to unsampled pages are unresolved, not corpus defects.',
    }
    course = 'Baseline Study'
    repo.create_course(course, workspace='studypal')
    inbox = root / 'runtime' / 'StudyPal' / course / 'inbox'
    inbox.mkdir(parents=True, exist_ok=True)
    for path in (root / 'study-sample').iterdir():
        path.rename(inbox / path.name)
    scan = sources.scan_course(course, [inbox])
    before = counts()
    repeat = sources.scan_course(course, [inbox])
    after = counts()
    result['real_study_excerpt_sample'] = {
        'indexed': scan['indexed'], 'failed': scan['failed'],
        'repeat_unchanged': repeat['unchanged'], 'repeat_counts_stable': before == after,
        'counts_before_repeat': before, 'counts_after_repeat': after,
        'note': 'Existing extracted text only; not a fresh PDF/OCR or textbook completeness test.',
    }

    # Controlled cases use synthetic prose so outcomes have unambiguous oracles.
    fixture = root / 'synthetic'
    fixture.mkdir()
    a, b = fixture / 'A.md', fixture / 'B.md'
    a.write_text('# Alpha\n\n## 摘要\nBaseline alpha summary.\n\n[[B]]\n')
    b.write_text('# Beta\n\nBaseline beta definition.\n')
    def import_fixture():
        return wiki_import.import_pages(fixture, 'Baseline Controls', allow_api=True)
    import_fixture()
    cid = scalar("SELECT id FROM courses WHERE name='Baseline Controls'")
    kid = scalar("SELECT id FROM kp WHERE course_id=? AND record_id='wiki:A.md'", (cid,))
    result['new_wiki_has_document'] = scalar('SELECT count(*) FROM knowledge_documents WHERE kp_id=?', (kid,)) == 1
    document = documents.find_by_kp(kid)
    if document:
        document = documents.update(document['id'], title='User note', body_md='Protected user prose')
    else:
        document = documents.create('workpal', title='User note', body_md='Protected user prose', kp_id=kid)
    repo.update_kp(kid, {'gist': 'Protected manual summary'})
    repo.write_mastery(kid, '会用')
    import_fixture()
    result['manual_summary_survives_unchanged_import'] = scalar('SELECT gist FROM kp WHERE id=?', (kid,)) == 'Protected manual summary'
    result['mastery_survives_import'] = scalar('SELECT mastery FROM kp WHERE id=?', (kid,)) == '会用'
    result['document_survives_import'] = documents.get(document['id'])['body_md'] == 'Protected user prose'
    old_evidence = rows('SELECT e.id,e.content_hash FROM evidence e JOIN source_files s ON s.id=e.source_id WHERE s.course_id=?', (cid,))
    a.write_text('# Alpha\n\n## 摘要\nChanged alpha summary.\n\n[[B]]\n')
    import_fixture()
    current = {(row['id'], row['content_hash']) for row in rows('SELECT id,content_hash FROM evidence')}
    result['old_evidence_retained_after_update'] = all((row['id'], row['content_hash']) in current for row in old_evidence)
    # Adopt a persistent page ID before moving; path-only pages intentionally
    # cannot be recognized as moves from a similar title or identical content.
    a.write_text('---\nuid: baseline-alpha\n---\n' + a.read_text())
    import_fixture()
    (fixture / 'A.md').rename(fixture / 'Moved.md')
    before_nodes = scalar('SELECT count(*) FROM kp WHERE course_id=?', (cid,))
    import_fixture()
    result['file_rename_preserves_identity'] = scalar('SELECT count(*) FROM kp WHERE course_id=?', (cid,)) == before_nodes
    b.unlink()
    preview = wiki_import.import_pages(fixture, 'Baseline Controls', apply=False)
    result['removed_source_pending_review'] = any(
        item['kind'] == 'missing' and item['relative'] == 'B.md' and item['requires_review']
        for item in preview['changes'])
    import_fixture()
    result['removed_source_reconciled'] = scalar("SELECT count(*) FROM kp WHERE course_id=? AND record_id='wiki:B.md' AND deleted_at=''", (cid,)) == 0
    result['final_integrity'] = scalar('PRAGMA integrity_check')
    result['foreign_key_errors'] = len(rows('PRAGMA foreign_key_check'))
    (root / 'worker-report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))


def run(database, wiki_root, report):
    if report.exists() or report.is_symlink():
        raise ValueError('Choose a new report path; existing evidence is not overwritten')
    if (report.resolve().is_relative_to(wiki_root.resolve())
            or report.resolve().is_relative_to(database.resolve().parent)
            or any(path.is_symlink() for path in report.absolute().parents)):
        raise ValueError('Report must be outside input directories and symlink paths')
    with tempfile.TemporaryDirectory(prefix='mindock-graph-baseline-') as directory:
        root = Path(directory).resolve()
        readonly_snapshot(database, root / 'snapshot.db')
        with sqlite3.connect(root / 'snapshot.db') as conn:
            stats = inventory(conn)
            excerpts = conn.execute('''SELECT s.id,ch.content FROM source_files s
                JOIN courses c ON c.id=s.course_id JOIN workspaces w ON w.id=c.workspace_id
                JOIN source_chunks ch ON ch.source_id=s.id WHERE w.key='studypal' AND ch.stale=0
                ORDER BY s.id,ch.position''').fetchall()
        (root / 'study-sample').mkdir()
        chosen = []
        for source_id, content in excerpts:
            if sum(1 for sid, _ in chosen if sid == source_id) < 3:
                chosen.append((source_id, content))
            if len(chosen) >= 6:
                break
        if not chosen:
            raise ValueError('No StudyPal extracted text available for baseline')
        for index, (_, content) in enumerate(chosen):
            (root / 'study-sample' / f'excerpt-{index}.md').write_text(content, encoding='utf-8')

        # Reuse the product parser in read-only mode, in a subprocess rooted in
        # temporary storage. No app imports occur in the production inventory.
        env = {**os.environ, 'CYBERPAL_DATA_DIR': str(root / 'runtime'), 'PYTHONDONTWRITEBYTECODE': '1'}
        selection = subprocess.check_output([sys.executable, str(Path(__file__).resolve()),
            '--select', str(wiki_root.resolve())], env=env, text=True)
        paths = [Path(p) for p in json.loads(selection)]
        hashes = {path: fingerprint(path) for path in paths}
        for path in paths:
            target = root / 'wiki-sample' / path.relative_to(wiki_root.resolve())
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
        if not paths:
            raise ValueError('No Wiki pages available for baseline')
        subprocess.run([sys.executable, str(Path(__file__).resolve()), '--worker', str(root)],
                       env=env, check=True, timeout=180)
        result = {'format': 'mindock.graph-baseline.v1', 'inventory': stats,
                  'real_sample': {'wiki_pages': len(paths), 'study_excerpts': len(chosen),
                                  'study_sources': len({sid for sid, _ in chosen})},
                  'observations': json.loads((root / 'worker-report.json').read_text()),
                  'wiki_sample_source_unchanged': all(fingerprint(path) == digest for path, digest in hashes.items()),
                  'limits': ['No model calls, semantic accuracy or answer quality measured.',
                             'Production database read via SQLite read-only backup; all writes isolated.',
                             'Synthetic cases characterize defects; false is a gap, not a passing gate.',
                             'No LifePal corpus or original PDF extraction exercised.']}
        report.parent.mkdir(parents=True, exist_ok=True)
        with report.open('x') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path)
    parser.add_argument('--wiki-root', type=Path)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--worker', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--select', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args.worker)
    elif args.select:
        sys.path.insert(0, str(ROOT))
        from app.memory.wiki_import import iter_pages
        pages = iter_pages(args.select)
        # Deterministic spread across the corpus, capped at twelve pages.
        sample = pages[::max(1, len(pages) // 12)][:12]
        print(json.dumps([str(page.path) for page in sample]))
    elif args.database and args.wiki_root and args.report:
        run(args.database, args.wiki_root, args.report)
    else:
        parser.error('--database, --wiki-root and --report are required')
