"""Portable graph exchange and integrity checks."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime

from .db import connect
from . import domain_profiles, sources


def health(course: str | None = None) -> dict:
    conn = connect()
    try:
        args = []
        where = ''
        if course:
            where = ' WHERE co.name=?'
            args.append(course)
        nodes = conn.execute('''SELECT kp.id,kp.name,kp.kp_type,co.name AS course_name FROM kp
                                JOIN courses co ON co.id=kp.course_id''' + where, args).fetchall()
        ids = {row['id'] for row in nodes}
        edges = conn.execute('''SELECT e.id,e.from_kp,e.to_kp,e.kind
                               FROM kp_edges e JOIN kp a ON a.id=e.from_kp
                               JOIN kp b ON b.id=e.to_kp
                               JOIN courses ca ON ca.id=a.course_id
                               JOIN courses cb ON cb.id=b.course_id
                              WHERE (? IS NULL OR ca.name=? OR cb.name=?)''',
                            [course, course, course]).fetchall()
        profile = domain_profiles.public_profile(course or '')
        isolated = [dict(row) for row in nodes
                    if row['kp_type'] != 'section'
                    and not any(e['from_kp'] == row['id'] or e['to_kp'] == row['id'] for e in edges)]
        structural_placeholders = [dict(row) for row in nodes
                                   if row['kp_type'] == 'section'
                                   and not any(e['from_kp'] == row['id'] or e['to_kp'] == row['id'] for e in edges)]
        contract_rows = conn.execute('''SELECT kp.id,kp.name,kp.source,nm.domain,nm.status,nm.source_refs_json
                                       FROM kp JOIN courses co ON co.id=kp.course_id
                                       LEFT JOIN knowledge_node_meta nm ON nm.kp_id=kp.id
                                       WHERE (? IS NULL OR co.name=?)''', [course, course]).fetchall()
        contract_gaps = []
        for row in contract_rows:
            if not row['domain'] or row['domain'] != profile['id']:
                contract_gaps.append({'id': row['id'], 'name': row['name'], 'issue': '领域合同未匹配'})
            elif not json.loads(row['source_refs_json'] or '[]') and not str(row['source'] or '').strip():
                contract_gaps.append({'id': row['id'], 'name': row['name'], 'issue': '缺少溯源'})
        duplicates = [dict(row) for row in conn.execute('''SELECT co.name AS course_name,kp.name,COUNT(*) AS count
                                                            FROM kp JOIN courses co ON co.id=kp.course_id
                                                           WHERE (? IS NULL OR co.name=?)
                                                           GROUP BY kp.course_id,kp.name HAVING COUNT(*)>1''',
                                                         [course, course]).fetchall()]
        bad_edges = [dict(row) for row in conn.execute('''SELECT id,from_kp,to_kp,kind FROM kp_edges
                                                          WHERE from_kp NOT IN (SELECT id FROM kp)
                                                             OR to_kp NOT IN (SELECT id FROM kp)''').fetchall()]
        empty_relations = [dict(row) for row in conn.execute("SELECT id,from_name,to_name,kind,status FROM relation_candidates WHERE status='pending' AND (trim(from_name)='' OR trim(to_name)='')").fetchall()]
        unresolved = [dict(row) for row in conn.execute("""SELECT bl.id,b.course_a,b.kp_a,b.course_b,b.kp_b,bl.status
                                                            FROM bridge_links bl JOIN bridges b ON b.id=bl.bridge_id
                                                           WHERE bl.status!='resolved'""").fetchall()] if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bridge_links'").fetchone() else []
        result = {'course': course or '全部课程', 'profile': profile, 'node_count': len(nodes), 'edge_count': len(edges),
                  'isolated_nodes': isolated, 'structural_placeholders': structural_placeholders,
                  'duplicate_nodes': duplicates,
                  'dangling_edges': bad_edges, 'empty_relations': empty_relations,
                  'unresolved_bridges': unresolved, 'contract_gaps': contract_gaps,
                  'contract_ok': not contract_gaps,
                  'ok': not (duplicates or bad_edges or empty_relations)}
    finally:
        conn.close()
    # T2.13a: the legacy fields stay for the graph panel; the read-only
    # consistency report is additive and never repairs anything.
    report = check_graph_health(course)
    result['consistency'] = report
    result['consistency_ok'] = report['ok']
    return result


def export_graph(course: str | None = None) -> dict:
    conn = connect()
    try:
        nodes = [dict(row) for row in conn.execute('''SELECT kp.*,co.name AS course_name FROM kp
                                                      JOIN courses co ON co.id=kp.course_id
                                                     WHERE (? IS NULL OR co.name=?) ORDER BY kp.id''', [course, course]).fetchall()]
        ids = {row['id'] for row in nodes}
        edges = [dict(row) for row in conn.execute('SELECT * FROM kp_edges ORDER BY id').fetchall()
                 if row['from_kp'] in ids and row['to_kp'] in ids]
        return {'schema': 'cyberpal.graph.v1', 'course': course or '全部课程',
                'nodes': nodes, 'edges': edges}
    finally:
        conn.close()


def import_graph(payload: dict, *, course: str | None = None) -> dict:
    if not isinstance(payload, dict) or payload.get('schema') not in (None, 'cyberpal.graph.v1'):
        raise ValueError('图谱文件 schema 不受支持')
    nodes = payload.get('nodes') or []
    edges = payload.get('edges') or []
    if not isinstance(nodes, list) or not isinstance(edges, list) or len(nodes) > 10000 or len(edges) > 20000:
        raise ValueError('图谱节点或关系数量超出限制')
    # T2.9/O-DATA-7: a graph import writes the formal graph through the audited
    # maintenance channel (allow_api marks the user-triggered /graph/import path).
    channel = sources.authorize_formal_write('import_graph', allow_api=True)
    conn = connect()
    try:
        name_by_id: dict[str, int] = {}
        course_by_id: dict[int, tuple[int, str]] = {}
        created = 0
        conn.execute('SAVEPOINT graph_import')
        for item in nodes:
            if not isinstance(item, dict) or not str(item.get('name') or '').strip():
                continue
            target_course = str(item.get('course_name') or course or payload.get('course') or '').strip()
            if not target_course or target_course == '全部课程':
                raise ValueError('每个图谱节点需要 course_name，或导入时指定 course')
            row = conn.execute(
                '''SELECT c.id,COALESCE(w.key,'studypal') AS workspace_key
                     FROM courses c LEFT JOIN workspaces w ON w.id=c.workspace_id
                    WHERE c.name=?''', (target_course,)).fetchone()
            if not row:
                raise ValueError(f'课程不存在：{target_course}')
            existing = conn.execute('SELECT id FROM kp WHERE course_id=? AND name=?', (row['id'], item['name'].strip())).fetchone()
            if existing:
                kp_id = existing['id']
            else:
                cur = conn.execute('''INSERT INTO kp(course_id,name,band,chapter,gist,detail,trap,source,related,kp_type,freq,mastery)
                                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                                    (row['id'], item['name'].strip(), str(item.get('band') or ''), str(item.get('chapter') or ''),
                                     str(item.get('gist') or ''), str(item.get('detail') or ''), str(item.get('trap') or ''),
                                     str(item.get('source') or ''), str(item.get('related') or ''), str(item.get('kp_type') or ''),
                                     str(item.get('freq') or ''), str(item.get('mastery') or '未读')))
                kp_id = cur.lastrowid
                created += 1
            course_by_id[int(kp_id)] = (int(row['id']), str(row['workspace_key']))
            if item.get('id') is not None:
                key = str(item['id'])
                mapped = name_by_id.get(key)
                if mapped is not None and mapped != kp_id:
                    raise ValueError(f'图谱节点外部标识重复：{key}')
                name_by_id[key] = kp_id
            if item.get('node_key'):
                key = str(item['node_key'])
                mapped = name_by_id.get(key)
                if mapped is not None and mapped != kp_id:
                    raise ValueError(f'图谱节点外部标识重复：{key}')
                name_by_id[key] = kp_id
        edge_count = 0
        for item in edges:
            if not isinstance(item, dict):
                raise ValueError('图谱关系格式无效')
            a = name_by_id.get(str(item.get('from_kp')))
            b = name_by_id.get(str(item.get('to_kp')))
            if a is None or b is None:
                raise ValueError('图谱关系端点必须属于本次导入的节点')
            if a == b:
                raise ValueError('知识点不能连接自己')
            course_a, workspace_a = course_by_id[a]
            course_b, workspace_b = course_by_id[b]
            if workspace_a != workspace_b:
                raise ValueError('图谱关系两端必须属于同一工作区')
            if course_a != course_b:
                raise ValueError('当前图谱关系两端必须属于同一课程')
            kind = str(item.get('kind') or 'related')
            if kind not in {'pre', 'related', 'part_of', 'supports', 'example_of', 'causes', 'contrasts'}:
                raise ValueError(f'不支持的图谱关系类型：{kind}')
            cur = conn.execute(
                'INSERT OR IGNORE INTO kp_edges(from_kp,to_kp,kind) VALUES(?,?,?)',
                (a, b, kind))
            edge_count += max(0, cur.rowcount)
        conn.execute('RELEASE SAVEPOINT graph_import')
        conn.commit()
        course_id = None
        if course and course != '全部课程':
            row = conn.execute('SELECT id FROM courses WHERE name=?', (course,)).fetchone()
            if row:
                course_id = int(row['id'])
        sources.record_formal_write(
            channel, course_id=course_id, nodes=created, edges=edge_count,
            detail=json.dumps({'nodes_created': created, 'edges_added': edge_count},
                              ensure_ascii=False))
        return {'ok': True, 'nodes_created': created, 'edges_added': edge_count}
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()

# ── T2.13a: read-only graph / mastery consistency inspection ───────────
MASTERY_VALUES = ('熟练', '会用', '读过', '未读')


def _issue(kind: str, severity: str, rows, hint: str, limit: int = 5) -> dict:
    items = [dict(row) for row in rows]
    return {'kind': kind, 'severity': severity, 'count': len(items),
            'samples': items[:limit], 'hint': hint}


def _expected_node_key(course_id: int, name: str) -> str:
    """Mirror repo._node_key; duplicated locally to keep this module light."""
    normalized = ' '.join(str(name or '').split()).casefold()
    return 'kp_' + hashlib.sha256(
        f'cyberpal:{course_id}:{normalized}'.encode('utf-8')).hexdigest()[:24]


def _table_exists(conn, name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def check_graph_health(course: str | None = None) -> dict:
    """Return the read-only consistency report used by /graph/health and the CLI.

    Categories: orphan/duplicate nodes, identity drift, bridge links, relation
    candidates, node metadata/evidence refs, mastery values, chapter keys,
    soft-deleted nodes still referenced, and search-index drift. The function
    never writes: callers assert PRAGMA data_version is unchanged.
    """
    conn = connect()
    issues: list[dict] = []
    hints: list[str] = []
    counts: dict = {}
    scope = ''
    scope_args: list = []
    course_row = None
    try:
        # Empty or not-yet-migrated database: report the degradation, never crash.
        if not _table_exists(conn, 'kp') or not _table_exists(conn, 'courses'):
            return {'course': course or '全部课程',
                    'ok': True,
                    'checked_at': datetime.now().isoformat(timespec='seconds'),
                    'counts': {},
                    'issues': [],
                    'hints': ['数据库未初始化（缺少 kp/courses 表），已跳过一致性检查'],
                    'totals': {'errors': 0, 'warnings': 0}}
        if course:
            course_row = conn.execute(
                'SELECT id,name FROM courses WHERE name=?', (course,)).fetchone()
            if course_row:
                scope = ' AND co.id=?'
                scope_args = [int(course_row['id'])]
            else:
                issues.append(_issue('course_missing', 'error',
                                     [{'course': course}], '巡检指定的课程不存在'))
        live = "COALESCE(kp.deleted_at,'')=''"
        kp_scope = 'FROM kp JOIN courses co ON co.id=kp.course_id WHERE ' + live + scope
        counts['kp'] = int(conn.execute(
            'SELECT COUNT(*) ' + kp_scope, scope_args).fetchone()[0])
        counts['edges'] = int(conn.execute(
            'SELECT COUNT(*) FROM kp_edges').fetchone()[0])
        counts['bridges'] = int(conn.execute(
            'SELECT COUNT(*) FROM bridges').fetchone()[0]) if _table_exists(conn, 'bridges') else 0
        counts['bridge_links'] = int(conn.execute(
            'SELECT COUNT(*) FROM bridge_links').fetchone()[0]) if _table_exists(conn, 'bridge_links') else 0
        counts['relation_candidates'] = int(conn.execute(
            'SELECT COUNT(*) FROM relation_candidates').fetchone()[0])
        counts['knowledge_node_meta'] = int(conn.execute(
            'SELECT COUNT(*) FROM knowledge_node_meta').fetchone()[0])

        # ① kp without an existing course (FK normally prevents this).
        rows = conn.execute('''SELECT kp.id,kp.name,kp.course_id FROM kp
                              LEFT JOIN courses co ON co.id=kp.course_id
                              WHERE co.id IS NULL''').fetchall()
        if rows:
            issues.append(_issue('kp_missing_course', 'error', rows,
                                 '知识点引用的课程不存在（外键被绕过）'))

        # ② duplicate names per course (UNIQUE prevents new ones; legacy DBs may drift).
        rows = conn.execute('''SELECT co.name AS course_name,kp.name,COUNT(*) AS count
                              FROM kp JOIN courses co ON co.id=kp.course_id
                              WHERE ''' + live + scope + '''
                              GROUP BY kp.course_id,kp.name HAVING COUNT(*)>1''',
                            scope_args).fetchall()
        if rows:
            issues.append(_issue('duplicate_kp_name', 'error', rows,
                                 '同一课程出现重名知识点'))
        rows = conn.execute('''SELECT co.name AS course_name,ki.node_key,COUNT(*) AS count
                              FROM kp_identities ki JOIN kp ON kp.id=ki.kp_id
                              JOIN courses co ON co.id=kp.course_id WHERE 1=1''' + scope +
                            ''' GROUP BY ki.node_key HAVING COUNT(*)>1''',
                            scope_args).fetchall()
        if rows:
            issues.append(_issue('identity_key_conflict', 'error', rows,
                                 'node_key 重复（identity 表约束应已阻止）'))

        # ③ identity missing / drift.
        rows = conn.execute('''SELECT kp.id,kp.name,co.name AS course_name
                              FROM kp JOIN courses co ON co.id=kp.course_id
                              LEFT JOIN kp_identities ki ON ki.kp_id=kp.id
                              WHERE ''' + live + ' AND ki.kp_id IS NULL' + scope,
                            scope_args).fetchall()
        if rows:
            issues.append(_issue('identity_missing', 'warning', rows,
                                 '知识点没有稳定 identity，改名前需先补 node_key'))
        rows = conn.execute('''SELECT kp.id,kp.name,kp.course_id,co.name AS course_name,
                                     ki.node_key AS stored_key
                              FROM kp JOIN courses co ON co.id=kp.course_id
                              JOIN kp_identities ki ON ki.kp_id=kp.id
                              WHERE ''' + live + scope, scope_args).fetchall()
        drift = []
        for row in rows:
            if row['stored_key'] != _expected_node_key(int(row['course_id']), row['name']):
                drift.append({'id': row['id'], 'name': row['name'],
                              'course_name': row['course_name'],
                              'stored_key': row['stored_key'],
                              'expected_key': _expected_node_key(int(row['course_id']), row['name'])});
        if drift:
            issues.append(_issue('identity_drift', 'warning', drift,
                                 'node_key 与 course+name 的确定性计算结果不一致'))

        # ④ bridge links: unresolved, missing kp, node_key mismatch.
        if _table_exists(conn, 'bridge_links') and _table_exists(conn, 'bridges'):
            unresolved = conn.execute('''SELECT bl.id,bl.bridge_id,bl.status,b.course_a,b.kp_a,
                                               b.course_b,b.kp_b
                                        FROM bridge_links bl JOIN bridges b ON b.id=bl.bridge_id
                                        WHERE bl.status!='resolved' ''').fetchall()
            if unresolved:
                issues.append(_issue('bridge_unresolved', 'warning', unresolved,
                                     '跨课桥接尚未解析到正式节点，可运行维护命令'))
            missing = conn.execute('''SELECT bl.id,bl.bridge_id,a_kp.id AS a_kp_id,b_kp.id AS b_kp_id,
                                            bl.a_kp_id AS a_ref,bl.b_kp_id AS b_ref
                                     FROM bridge_links bl
                                     LEFT JOIN kp a_kp ON a_kp.id=bl.a_kp_id
                                     LEFT JOIN kp b_kp ON b_kp.id=bl.b_kp_id
                                     WHERE (bl.a_kp_id IS NOT NULL AND a_kp.id IS NULL)
                                        OR (bl.b_kp_id IS NOT NULL AND b_kp.id IS NULL)''').fetchall()
            if missing:
                issues.append(_issue('bridge_kp_missing', 'error', missing,
                                     'bridge_links 引用的知识点不存在'))
            key_drift = conn.execute('''SELECT bl.id,bl.bridge_id,bl.a_node_key,bl.b_node_key,
                                             ka.name AS a_name,kb.name AS b_name,
                                             kia.node_key AS a_key,kib.node_key AS b_key
                                      FROM bridge_links bl
                                      JOIN kp ka ON ka.id=bl.a_kp_id
                                      JOIN kp kb ON kb.id=bl.b_kp_id
                                      LEFT JOIN kp_identities kia ON kia.kp_id=ka.id
                                      LEFT JOIN kp_identities kib ON kib.kp_id=kb.id
                                      WHERE (bl.a_node_key!='' AND bl.a_node_key!=COALESCE(kia.node_key,''))
                                         OR (bl.b_node_key!='' AND bl.b_node_key!=COALESCE(kib.node_key,''))''').fetchall()
            if key_drift:
                issues.append(_issue('bridge_node_key_drift', 'warning', key_drift,
                                     'bridge_links 缓存的 node_key 与 identity 不一致'))
        else:
            hints.append('bridge_links/bridges 表不存在，已跳过桥梁检查')

        # ⑤ relation candidates: dangling kp or stale evidence.
        rows = conn.execute('''SELECT rc.id,rc.from_kp,rc.to_kp,rc.status,rc.evidence_id
                              FROM relation_candidates rc
                              WHERE (rc.from_kp IS NOT NULL AND rc.from_kp NOT IN (SELECT id FROM kp))
                                 OR (rc.to_kp IS NOT NULL AND rc.to_kp NOT IN (SELECT id FROM kp))'''
                            ).fetchall()
        if rows:
            issues.append(_issue('candidate_kp_missing', 'error', rows,
                                 '关系候选引用的知识点不存在'))
        stale = conn.execute('''SELECT rc.id,rc.status,rc.evidence_id,e.id AS live_evidence_id,e.stale
                               FROM relation_candidates rc
                               LEFT JOIN evidence e ON e.id=rc.evidence_id
                               WHERE rc.status='pending' AND rc.evidence_id IS NOT NULL
                                 AND (e.id IS NULL OR COALESCE(e.stale,0)!=0)''').fetchall()
        if stale:
            issues.append(_issue('candidate_stale_evidence', 'warning', stale,
                                 '待审核关系候选引用了失效/缺失证据（读路径已隐藏，需重扫确认）'))

        # ⑥ metadata missing / invalid JSON / stale evidence refs.
        rows = conn.execute('''SELECT kp.id,kp.name,co.name AS course_name
                              FROM kp JOIN courses co ON co.id=kp.course_id
                              LEFT JOIN knowledge_node_meta nm ON nm.kp_id=kp.id
                              WHERE ''' + live + ' AND nm.kp_id IS NULL' + scope,
                            scope_args).fetchall()
        if rows:
            issues.append(_issue('meta_missing', 'warning', rows,
                                 '知识点缺少 knowledge_node_meta（领域合同/溯源缺失）'))
        meta_rows = conn.execute('''SELECT nm.kp_id,nm.source_refs_json,nm.attributes_json
                                    FROM knowledge_node_meta nm JOIN kp ON kp.id=nm.kp_id
                                    JOIN courses co ON co.id=kp.course_id
                                    WHERE ''' + live + scope, scope_args).fetchall()
        invalid_json = []
        stale_refs = []
        live_evidence = {int(row[0]) for row in conn.execute(
            'SELECT id FROM evidence WHERE COALESCE(stale,0)=0').fetchall()}
        for row in meta_rows:
            bad_fields: list[str] = []
            refs = None
            try:
                refs = json.loads(row['source_refs_json'] or '[]')
            except (TypeError, ValueError):
                bad_fields.append('source_refs_json')
            try:
                json.loads(row['attributes_json'] or '{}')
            except (TypeError, ValueError):
                bad_fields.append('attributes_json')
            if bad_fields:
                invalid_json.append({'kp_id': row['kp_id'], 'fields': bad_fields})
            bad = []
            if isinstance(refs, list):
                for ref in refs:
                    evidence_id = ref.get('evidence_id') if isinstance(ref, dict) else None
                    if evidence_id is None:
                        continue
                    try:
                        if int(evidence_id) not in live_evidence:
                            bad.append(evidence_id)
                    except (TypeError, ValueError):
                        bad.append(evidence_id)
            if bad:
                stale_refs.append({'kp_id': row['kp_id'], 'missing_or_stale': bad[:5]})
        if invalid_json:
            issues.append(_issue('meta_invalid_json', 'warning', invalid_json,
                                 'knowledge_node_meta 的 JSON 字段不可解析'))
        if stale_refs:
            issues.append(_issue('meta_stale_refs', 'warning', stale_refs,
                                 'source_refs 指向不存在或已淘汰的证据'))

        # ⑦ mastery values outside the four documented levels.
        rows = conn.execute('''SELECT kp.id,kp.name,kp.mastery,co.name AS course_name
                              FROM kp JOIN courses co ON co.id=kp.course_id
                              WHERE ''' + live + " AND kp.mastery NOT IN (?,?,?,?)" + scope,
                            [*MASTERY_VALUES, *scope_args]).fetchall()
        if rows:
            issues.append(_issue('invalid_mastery', 'error', rows,
                                 '掌握度只能是 熟练/会用/读过/未读'))

        # ⑧ chapter_catalog / chapter_sections keys vs kp navigation fields.
        unknown_chapter = conn.execute('''SELECT kp.id,kp.name,kp.chapter,kp.chapter_key,
                                                co.name AS course_name
                                         FROM kp JOIN courses co ON co.id=kp.course_id
                                         WHERE ''' + live + " AND kp.chapter_key!=''" + scope +
                                       ''' AND NOT EXISTS (SELECT 1 FROM chapter_catalog cc
                                              WHERE cc.course_id=kp.course_id
                                                AND cc.chapter_key=kp.chapter_key
                                                AND cc.status='active')''',
                                     scope_args).fetchall()
        if unknown_chapter:
            issues.append(_issue('chapter_key_unknown', 'warning', unknown_chapter,
                                 'kp.chapter_key 找不到 active 的 chapter_catalog 行'))
        mismatch = []
        chapter_rows = conn.execute('''SELECT kp.id,kp.name,kp.chapter,kp.chapter_key,
                                             co.name AS course_name
                                      FROM kp JOIN courses co ON co.id=kp.course_id
                                      WHERE ''' + live + scope, scope_args).fetchall()
        from . import chapter_catalog
        for row in chapter_rows:
            number = chapter_catalog.chapter_number(str(row['chapter'] or ''))
            if number is None:
                continue
            expected = chapter_catalog.chapter_key(number)
            if row['chapter_key'] and row['chapter_key'] != expected:
                mismatch.append({'id': row['id'], 'name': row['name'],
                                 'chapter': row['chapter'],
                                 'chapter_key': row['chapter_key'],
                                 'expected_key': expected})
        if mismatch:
            issues.append(_issue('chapter_key_mismatch', 'warning', mismatch,
                                 'kp.chapter 文本与 chapter_key 不一致'))
        unknown_section = conn.execute('''SELECT kp.id,kp.name,kp.section_key,co.name AS course_name
                                         FROM kp JOIN courses co ON co.id=kp.course_id
                                         WHERE ''' + live + " AND kp.section_key!=''" + scope +
                                       ''' AND NOT EXISTS (SELECT 1 FROM chapter_sections cs
                                              JOIN chapter_catalog cc ON cc.id=cs.chapter_id
                                              WHERE cc.course_id=kp.course_id
                                                AND cs.section_key=kp.section_key
                                                AND cs.status='active')''',
                                     scope_args).fetchall()
        if unknown_section:
            issues.append(_issue('section_key_unknown', 'warning', unknown_section,
                                 'kp.section_key 找不到 active 的 chapter_sections 行'))

        # ⑨ soft-deleted kp still referenced by graph structures.
        deleted = conn.execute("SELECT id,name FROM kp WHERE COALESCE(deleted_at,'')!=''").fetchall()
        deleted_ids = {int(row['id']) for row in deleted}
        referenced: list[dict] = []
        if deleted_ids:
            edges_ref = conn.execute('''SELECT from_kp,to_kp FROM kp_edges
                                        WHERE from_kp IN (SELECT id FROM kp WHERE COALESCE(deleted_at,'')!='')
                                           OR to_kp IN (SELECT id FROM kp WHERE COALESCE(deleted_at,'')!='')''').fetchall()
            for row in edges_ref:
                referenced.append({'kind': 'kp_edge', 'from_kp': row['from_kp'],
                                   'to_kp': row['to_kp']})
            if _table_exists(conn, 'bridge_links'):
                for row in conn.execute('''SELECT id,a_kp_id,b_kp_id FROM bridge_links
                                           WHERE a_kp_id IN (SELECT id FROM kp WHERE COALESCE(deleted_at,'')!='')
                                              OR b_kp_id IN (SELECT id FROM kp WHERE COALESCE(deleted_at,'')!='')''').fetchall():
                    referenced.append({'kind': 'bridge_link', 'id': row['id'],
                                       'a_kp_id': row['a_kp_id'], 'b_kp_id': row['b_kp_id']})
            for row in conn.execute('''SELECT id,status,from_kp,to_kp FROM relation_candidates
                                       WHERE from_kp IN (SELECT id FROM kp WHERE COALESCE(deleted_at,'')!='')
                                          OR to_kp IN (SELECT id FROM kp WHERE COALESCE(deleted_at,'')!='')''').fetchall():
                referenced.append({'kind': 'relation_candidate', 'id': row['id'],
                                   'status': row['status'], 'from_kp': row['from_kp'],
                                   'to_kp': row['to_kp']})
        if referenced:
            issues.append(_issue('deleted_kp_referenced', 'error', referenced,
                                 '软删知识点仍被关系/桥接/候选引用，需先迁移或恢复'))

        # ⑩ search index drift (only when FTS is readable).
        try:
            from .db import sqlite_capabilities
            from . import search_index
            if not sqlite_capabilities().get('fts5'):
                hints.append('本机 SQLite 无 FTS5，已跳过检索索引计数检查')
            else:
                actual = {row[0]: int(row[1]) for row in conn.execute(
                    'SELECT kind,COUNT(*) FROM search_fts GROUP BY kind').fetchall()}
                expected = {
                    'kp': int(conn.execute('''SELECT COUNT(*) FROM kp JOIN courses co ON co.id=kp.course_id
                                             WHERE ''' + live + ''' AND NOT EXISTS (
                                               SELECT 1 FROM source_files dev
                                                WHERE dev.source_kind='development_doc'
                                                  AND dev.path=kp.source)''' + scope,
                                           scope_args).fetchone()[0]),
                    'source_chunk': int(conn.execute('''SELECT COUNT(*) FROM source_chunks sc
                                                        JOIN source_files sf ON sf.id=sc.source_id
                                                        JOIN courses co ON co.id=sf.course_id
                                                        WHERE sf.status='indexed' AND sc.stale=0
                                                          AND sf.source_kind!='development_doc' ''' + scope,
                                                      scope_args).fetchone()[0]),
                    'evidence': int(conn.execute('''SELECT COUNT(*) FROM evidence e
                                                   JOIN source_files sf ON sf.id=e.source_id
                                                   JOIN courses co ON co.id=sf.course_id
                                                   WHERE sf.status='indexed' AND e.stale=0
                                                     AND sf.source_kind!='development_doc' ''' + scope,
                                                 scope_args).fetchone()[0]),
                    'event': int(conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]),
                    'notice': int(conn.execute('SELECT COUNT(*) FROM notices').fetchone()[0]),
                    'progress': int(conn.execute('SELECT COUNT(*) FROM progress').fetchone()[0]),
                }
                drift = [{'kind': key, 'expected': expected.get(key, 0),
                          'actual': actual.get(key, 0)}
                         for key in sorted(expected)
                         if expected.get(key, 0) != actual.get(key, 0)]
                if drift:
                    issues.append(_issue('search_fts_drift', 'warning', drift,
                                         'search_fts 计数与正式表不一致，运行 /api/search/rebuild'))
                counts['search_fts'] = sum(actual.values())
        except Exception as exc:
            hints.append('检索索引检查失败：' + str(exc)[:120])
    finally:
        conn.close()
    errors = [item for item in issues if item['severity'] == 'error']
    warnings = [item for item in issues if item['severity'] == 'warning']
    counts['issues'] = len(issues)
    return {'course': course or '全部课程',
            'ok': not errors,
            'checked_at': datetime.now().isoformat(timespec='seconds'),
            'counts': counts,
            'issues': issues,
            'hints': hints,
            'totals': {'errors': len(errors), 'warnings': len(warnings)}}
