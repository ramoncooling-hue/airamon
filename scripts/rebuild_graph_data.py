"""Repair the initial textbook graph without touching raw course materials."""

from __future__ import annotations

import json

from app.memory import chapter_catalog, db, repo, sources


def reject_legacy_relation_candidates(conn, course_id: int) -> int:
    """Retire pre-governance cartesian candidates without deleting audit history."""
    rows = conn.execute(
        '''SELECT rc.id FROM relation_candidates rc
           JOIN source_files sf ON sf.id=rc.source_id
           WHERE sf.course_id=? AND rc.status='pending'
             AND (rc.reason=? OR rc.from_kp IS NULL OR rc.to_kp IS NULL)''',
        (course_id, '同一份材料中的已确认节点，待人工审核'),
    ).fetchall()
    if not rows:
        return 0
    with conn:
        conn.executemany(
            """UPDATE relation_candidates
               SET status='rejected', reason='旧版同材料自动配对候选，已撤销；请用证据关系或手工关系建立关联',
                   reviewed_at=datetime('now','localtime')
               WHERE id=?""",
            [(row['id'],) for row in rows],
        )
    return len(rows)


def main() -> None:
    db.apply_schema()
    # T2.9/O-DATA-7: repairs write the formal graph only through the audited
    # maintenance channel; the ingest_runs row records script/counts/time.
    channel = sources.authorize_formal_write('rebuild_graph_data')
    conn = db.connect()
    conn.execute('PRAGMA foreign_keys=ON')
    course = conn.execute("SELECT id FROM courses WHERE name='会计学'").fetchone()
    if not course:
        raise SystemExit('会计学课程不存在')
    course_id = course['id']
    retired_candidates = reject_legacy_relation_candidates(conn, course_id)
    section_anchors = repo.backfill_section_nodes('会计学')

    confirmed = conn.execute(
        '''SELECT cr.target_kp_id,kc.chapter,sf.id AS source_id
           FROM candidate_reviews cr
           JOIN knowledge_candidates kc ON kc.id=cr.candidate_id
           JOIN source_files sf ON sf.id=kc.source_id
           WHERE cr.action='confirmed' AND cr.target_kp_id IS NOT NULL AND sf.course_id=?''',
        (course_id,),
    ).fetchall()
    repaired = 0
    for row in confirmed:
        number = chapter_catalog.chapter_number(row['chapter'])
        if not number:
            continue
        chapter = conn.execute(
            'SELECT start_locator,end_locator FROM chapter_catalog WHERE course_id=? AND number=?',
            (course_id, number),
        ).fetchone()
        if not chapter:
            continue
        evidence = conn.execute(
            '''SELECT id FROM evidence
               WHERE source_id=? AND locator_start>=? AND locator_start<=?
               ORDER BY id LIMIT 20''',
            (row['source_id'], chapter['start_locator'], chapter['end_locator']),
        ).fetchall()
        if not evidence:
            continue
        conn.execute('DELETE FROM kp_evidence WHERE kp_id=?', (row['target_kp_id'],))
        conn.executemany(
            'INSERT OR IGNORE INTO kp_evidence(kp_id,evidence_id,role) VALUES(?,?,?)',
            [(row['target_kp_id'], item['id'], 'supports') for item in evidence],
        )
        conn.execute(
            '''INSERT INTO knowledge_node_meta
                (kp_id,schema_version,domain,source_refs_json,status)
               VALUES(?,?,?,?, 'confirmed')
               ON CONFLICT(kp_id) DO UPDATE SET
                 source_refs_json=excluded.source_refs_json,
                 updated_at=datetime('now','localtime')''',
            (
                row['target_kp_id'], 'cyberpal.knowledge.v1', 'mba',
                json.dumps([{'evidence_id': item['id']} for item in evidence], ensure_ascii=False),
            ),
        )
        repaired += 1

    structural = []
    for row in conn.execute(
        '''SELECT kp.id,kp.name,kp.chapter,kp.kp_type
           FROM kp JOIN courses c ON c.id=kp.course_id WHERE c.id=?''',
        (course_id,),
    ).fetchall():
        if chapter_catalog.is_structural_node(dict(row)):
            structural.append(dict(row))
    if structural:
        ids = [row['id'] for row in structural]
        placeholders = ','.join('?' for _ in ids)
        conn.execute(f'DELETE FROM kp WHERE id IN ({placeholders})', ids)
    for row in conn.execute(
        '''SELECT kc.id,kc.name FROM knowledge_candidates kc
           JOIN source_files sf ON sf.id=kc.source_id
           WHERE sf.course_id=? AND kc.chapter='来源材料' ''',
        (course_id,),
    ).fetchall():
        if str(row['name']).startswith('第'):
            conn.execute("UPDATE knowledge_candidates SET status='rejected' WHERE id=?", (row['id'],))
            conn.execute(
                '''INSERT INTO candidate_reviews(candidate_id,action,note)
                   VALUES(?,?,?)''',
                (row['id'], 'rejected', '教材章节标题已进入章节目录，不作为正式知识点'),
            )

    # Tighten evidence and section ownership using the actual textbook pages.
    chapter_rows = {
        row['chapter_key']: dict(row)
        for row in conn.execute(
            'SELECT id,chapter_key,start_locator,end_locator FROM chapter_catalog WHERE course_id=?',
            (course_id,),
        ).fetchall()
    }
    sections_by_chapter = {}
    for row in conn.execute(
        '''SELECT cs.*,cc.chapter_key FROM chapter_sections cs
           JOIN chapter_catalog cc ON cc.id=cs.chapter_id WHERE cc.course_id=?''',
        (course_id,),
    ).fetchall():
        sections_by_chapter.setdefault(row['chapter_key'], []).append(dict(row))
    node_evidence = {}
    for node in conn.execute(
        '''SELECT id,name,chapter_key,section_key,kp_type FROM kp
           WHERE course_id=? ORDER BY id''',
        (course_id,),
    ).fetchall():
        if node['kp_type'] == 'section':
            continue
        chapter = chapter_rows.get(node['chapter_key'])
        if not chapter:
            continue
        evidence = conn.execute(
            '''SELECT id,locator_start,content FROM evidence
               WHERE source_id=(SELECT source_id FROM chapter_catalog WHERE id=? )
                 AND locator_start>=? AND locator_start<=?
                 AND content LIKE ? ORDER BY id LIMIT 8''',
            (chapter['id'], chapter['start_locator'], chapter['end_locator'], f"%{node['name']}%"),
        ).fetchall()
        ids = [item['id'] for item in evidence]
        if ids:
            node_evidence[node['id']] = set(ids)
            section_key = node['section_key']
            for section in sections_by_chapter.get(node['chapter_key'], []):
                section_evidence = conn.execute(
                    '''SELECT id FROM evidence
                       WHERE source_id=(SELECT source_id FROM chapter_catalog WHERE id=? )
                         AND locator_start>=? AND locator_start<=?
                         AND content LIKE ? LIMIT 1''',
                    (chapter['id'], section['start_locator'] or chapter['start_locator'],
                     section['end_locator'] or chapter['end_locator'], f"%{node['name']}%"),
                ).fetchone()
                if section_evidence:
                    section_key = section['section_key']
                    break
            conn.execute('UPDATE kp SET section_key=? WHERE id=?', (section_key, node['id']))
            conn.execute('DELETE FROM kp_evidence WHERE kp_id=?', (node['id'],))
            conn.executemany(
                'INSERT OR IGNORE INTO kp_evidence(kp_id,evidence_id,role) VALUES(?,?,?)',
                [(node['id'], evidence_id, 'supports') for evidence_id in ids],
            )
            conn.execute(
                'UPDATE knowledge_node_meta SET source_refs_json=?,updated_at=datetime(\'now\',\'localtime\') WHERE kp_id=?',
                (json.dumps([{'evidence_id': evidence_id} for evidence_id in ids], ensure_ascii=False), node['id']),
            )

    # Add a small number of reviewable, evidence-backed local associations.
    # Rebuild only this script's inferred edges. Confirmed and manually edited
    # relations stay intact, while a changed textbook can be re-inferred cleanly.
    conn.execute(
        '''DELETE FROM kp_edges
           WHERE id IN (
             SELECT rm.edge_id FROM knowledge_relation_meta rm
             WHERE rm.status='inferred'
               AND rm.reason='同一教材小节共享证据片段的初始化关联，建议复核'
           )'''
    )
    by_section = {}
    for node in conn.execute(
        "SELECT id,section_key FROM kp WHERE course_id=? AND section_key!=? AND kp_type!='section' ORDER BY id",
        (course_id, ''),
    ).fetchall():
        by_section.setdefault(node['section_key'], []).append(node['id'])
    added_edges = 0
    for node_ids in by_section.values():
        candidates = []
        for index, from_id in enumerate(node_ids):
            for to_id in node_ids[index + 1:]:
                shared = node_evidence.get(from_id, set()) & node_evidence.get(to_id, set())
                if shared:
                    candidates.append((len(shared), from_id, to_id, min(shared)))
        for _, from_id, to_id, evidence_id in sorted(candidates, reverse=True)[:max(1, len(node_ids) * 2)]:
            cur = conn.execute(
                'INSERT OR IGNORE INTO kp_edges(from_kp,to_kp,kind) VALUES(?,?,?)',
                (from_id, to_id, 'related'),
            )
            if cur.rowcount:
                edge_id = cur.lastrowid
                conn.execute(
                    '''INSERT OR IGNORE INTO knowledge_relation_meta
                       (edge_id,schema_version,evidence_id,reason,confidence,status)
                       VALUES(?,?,?,?,?,?)''',
                    (edge_id, 'cyberpal.knowledge.v1', evidence_id,
                     '同一教材小节共享证据片段的初始化关联，建议复核', 0.55, 'inferred'),
                )
                added_edges += 1
    conn.commit()
    sources.record_formal_write(
        channel, course_id=course_id,
        nodes=repaired + len(structural), edges=added_edges,
        detail=json.dumps({'repaired_nodes': repaired,
                           'removed_structural_nodes': len(structural),
                           'inferred_edges': added_edges}, ensure_ascii=False))
    conn.close()
    print({'repaired_nodes': repaired, 'removed_structural_nodes': len(structural),
           'inferred_edges': added_edges, 'retired_relation_candidates': retired_candidates,
           'section_anchors': section_anchors})


if __name__ == '__main__':
    main()
