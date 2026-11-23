"""Create review-only graph relations from explicit, evidence-backed Wiki links."""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..agent import llm
from .db import connect

ARROWS = re.compile(r'\s*(?:->|→|⇒|⇢)\s*')


def _names_in(text: str, names: list[tuple[str, int]]) -> list[int]:
    text = re.sub(r'\s*[（(]?证据[\s\d、，,;；—–-]*[）)]?', '', text).strip()
    if '：' in text or ':' in text:
        text = re.split(r'[：:]', text)[-1]
    lookup = {name: kp_id for name, kp_id in names if name}
    found = []
    for part in re.split(r'[、，,;；×+]', text):
        candidate = part.strip().strip(' \t\r\n。；:：()（）[]【】「」“”')
        if candidate in lookup:
            found.append(lookup[candidate])
    return list(dict.fromkeys(found))


def _evidence_refs(statement: str) -> list[int]:
    refs: list[int] = []
    for first, last in re.findall(r'证据\s*(\d+)(?:\s*[-—–]\s*(\d+))?', statement):
        start, end = int(first), int(last or first)
        if end - start <= 30:
            refs.extend(range(start, end + 1))
    return list(dict.fromkeys(refs))


def _dictionary_path(note_path: str) -> Path:
    note = Path(note_path)
    if note.parent.name == 'notes':
        return note.parent.parent / 'dictionary' / f'{note.stem}.json'
    return note.with_suffix('.json')


def _local_evidence(conn, source_id: int, item: dict) -> dict | None:
    # P2-3: dictionary drafts may lag behind a rescan; retired rows are history.
    return conn.execute(
        '''SELECT id,content FROM evidence
           WHERE source_id=? AND locator_type=? AND locator_start IS ? AND locator_end IS ?
             AND COALESCE(stale,0)=0
           ORDER BY id LIMIT 1''',
        (source_id, item.get('locator_type') or 'chunk', item.get('locator_start'), item.get('locator_end')),
    ).fetchone()


def generate(course: str) -> dict:
    """Turn explicit dictionary arrows into pending candidates, never formal edges."""
    conn = connect()
    result = {'created': 0, 'duplicates': 0, 'unmapped': 0, 'no_evidence': 0, 'dictionaries': 0}
    try:
        course_row = conn.execute('SELECT id FROM courses WHERE name=?', (course,)).fetchone()
        if not course_row:
            raise ValueError('课程不存在')
        course_id = course_row['id']
        node_rows = conn.execute(
            'SELECT id,name FROM kp WHERE course_id=? ORDER BY length(name) DESC,id', (course_id,)
        ).fetchall()
        names: list[tuple[str, int]] = [(row['name'], row['id']) for row in node_rows if row['name']]
        for row in conn.execute(
            '''SELECT ka.kp_id,ka.alias FROM kp_aliases ka JOIN kp k ON k.id=ka.kp_id
               WHERE k.course_id=? ORDER BY length(ka.alias) DESC''', (course_id,)
        ):
            if row['alias']:
                names.append((row['alias'], row['kp_id']))

        notes = conn.execute(
            '''SELECT kn.path,kn.source_id FROM knowledge_notes kn
               JOIN source_files sf ON sf.id=kn.source_id
               WHERE sf.course_id=? AND kn.status='assisted_draft'
               ORDER BY kn.id''', (course_id,)
        ).fetchall()
        for note in notes:
            path = _dictionary_path(note['path'])
            if not path.is_file():
                continue
            try:
                dictionary = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError):
                continue
            if dictionary.get('schema') != 'cyberpal.dictionary.v2' or dictionary.get('model_assisted') is not True:
                continue
            result['dictionaries'] += 1
            source_id = note['source_id']
            local_evidence = [
                (index, item, _local_evidence(conn, source_id, item))
                for index, item in enumerate(dictionary.get('evidence') or [], 1)
                if isinstance(item, dict)
            ]
            for statement in dictionary.get('links') or []:
                if not isinstance(statement, str):
                    continue
                segments = ARROWS.split(statement)
                if len(segments) < 2:
                    continue
                segment_ids = [_names_in(segment, names) for segment in segments]
                for index in range(len(segment_ids) - 1):
                    left_ids, right_ids = segment_ids[index], segment_ids[index + 1]
                    if not left_ids or not right_ids:
                        result['unmapped'] += 1
                        continue
                    for from_id in left_ids:
                        for to_id in right_ids:
                            if from_id == to_id:
                                continue
                            cited = set(_evidence_refs(statement))
                            matching = []
                            for ref, item, db_evidence in local_evidence:
                                if not db_evidence or (cited and ref not in cited):
                                    continue
                                content = str(db_evidence['content'] or '')
                                left_names = [name for name, kp_id in names if kp_id == from_id]
                                right_names = [name for name, kp_id in names if kp_id == to_id]
                                if any(name in content for name in left_names) and any(name in content for name in right_names):
                                    matching.append((ref, db_evidence['id']))
                            if not matching:
                                result['no_evidence'] += 1
                                continue
                            exists = conn.execute(
                                '''SELECT id FROM relation_candidates
                                   WHERE from_kp=? AND to_kp=? AND kind='related' LIMIT 1''',
                                (from_id, to_id),
                            ).fetchone()
                            edge_exists = conn.execute(
                                "SELECT id FROM kp_edges WHERE from_kp=? AND to_kp=? AND kind='related'",
                                (from_id, to_id),
                            ).fetchone()
                            if exists or edge_exists:
                                result['duplicates'] += 1
                                continue
                            ref, evidence_id = matching[0]
                            conn.execute(
                                '''INSERT INTO relation_candidates
                                   (source_id,from_kp,to_kp,from_name,to_name,kind,evidence_id,
                                    confidence,status,reason)
                                   VALUES(?,?,?,?,?,'related',?,0.85,'pending',?)''',
                                (source_id, from_id, to_id,
                                 next(name for name, kp_id in names if kp_id == from_id),
                                 next(name for name, kp_id in names if kp_id == to_id),
                                 evidence_id, f'Wiki 明确关系：{statement}（证据 {ref}）'),
                            )
                            result['created'] += 1
        conn.commit()
        return result
    finally:
        conn.close()


def _ai_payload(conn, course_id: int) -> tuple[list[dict], list[dict]]:
    rows = conn.execute(
        '''SELECT id,name,chapter,chapter_key,gist,detail,trap
           FROM kp WHERE course_id=? AND kp_type!='section' ORDER BY id''', (course_id,)
    ).fetchall()
    nodes = [dict(row) for row in rows]
    by_chapter: dict[str, list[dict]] = {}
    for node in nodes:
        by_chapter.setdefault(node.get('chapter_key') or node.get('chapter') or '', []).append(node)
    compact: list[dict] = []
    for chapter, items in by_chapter.items():
        compact.append({
            'chapter': chapter,
            'nodes': [{
                'name': item['name'],
                'chapter': item['chapter'],
                'gist': str(item.get('gist') or '')[:180],
                'detail': str(item.get('detail') or '')[:240],
            } for item in items[:28]],
        })
    evidence = [dict(row) for row in conn.execute(
        '''SELECT e.id,e.locator_start,e.content
           FROM evidence e JOIN source_files sf ON sf.id=e.source_id
           WHERE sf.course_id=? AND COALESCE(e.stale,0)=0
           ORDER BY e.locator_start,e.id LIMIT 120''', (course_id,)
    ).fetchall()]
    return compact, evidence


def generate_ai(course: str, *, limit: int = 20) -> dict:
    """Ask the configured fast model for cross-chapter drafts, then validate locally."""
    conn = connect()
    result = {'created': 0, 'duplicates': 0, 'unmapped': 0, 'no_evidence': 0, 'model_relations': 0}
    try:
        course_row = conn.execute('SELECT id FROM courses WHERE name=?', (course,)).fetchone()
        if not course_row:
            raise ValueError('课程不存在')
        nodes, evidence = _ai_payload(conn, course_row['id'])
        response = llm.chat('fast', [
            {'role': 'system', 'content': (
                '你是知识图谱审稿助手。只根据给定节点和证据提出跨章节候选。'
                '不要把同义词、共同出现、同属一个章节或仅仅都出现在目录中的概念当作关系。'
                'from_name 和 to_name 必须逐字来自节点清单，evidence_id 必须来自证据清单。'
                '只返回 JSON，不要 Markdown 围栏。'
            )},
            {'role': 'user', 'content': json.dumps({
                'task': f'为课程 {course} 提出最多 {max(1, min(limit, 40))} 条跨章节学习关系',
                'rules': [
                    'kind 只能是 pre 或 related',
                    'reason 说明具体的学习前置、方法依赖或概念联系',
                    '证据不足就不要提出',
                ],
                'chapters': nodes,
                'evidence': [
                    {'id': item['id'], 'page': item['locator_start'],
                     'text': str(item['content'] or '')[:500]}
                    for item in evidence
                ],
                'schema': {'relations': [{
                    'from_name': '节点名', 'to_name': '节点名',
                    'kind': 'pre', 'reason': '关系理由', 'evidence_id': 1,
                }]},
            }, ensure_ascii=False)},
        ], temperature=0.1)
        try:
            payload = json.loads(re.sub(r'^```(?:json)?\s*|\s*```$', '', response.strip(), flags=re.I))
        except json.JSONDecodeError as exc:
            raise ValueError('模型没有返回有效的关系候选 JSON') from exc
        relations = payload.get('relations') if isinstance(payload, dict) else []
        if not isinstance(relations, list):
            raise ValueError('模型关系候选不是数组')
        result['model_relations'] = len(relations)
        lookup = {row['name']: row for row in conn.execute(
            'SELECT id,name,chapter,chapter_key FROM kp WHERE course_id=?', (course_row['id'],)
        ).fetchall()}
        evidence_ids = {row['id'] for row in evidence}
        for item in relations[:max(1, min(limit, 40))]:
            if not isinstance(item, dict):
                continue
            from_name = str(item.get('from_name') or '').strip()
            to_name = str(item.get('to_name') or '').strip()
            kind = str(item.get('kind') or 'related').strip()
            if from_name not in lookup or to_name not in lookup or from_name == to_name:
                result['unmapped'] += 1
                continue
            left, right = lookup[from_name], lookup[to_name]
            left_chapter = left['chapter_key'] or left['chapter']
            right_chapter = right['chapter_key'] or right['chapter']
            if left_chapter and left_chapter == right_chapter:
                result['unmapped'] += 1
                continue
            if kind not in {'pre', 'related'}:
                kind = 'related'
            try:
                evidence_id = int(item.get('evidence_id'))
            except (TypeError, ValueError):
                result['no_evidence'] += 1
                continue
            if evidence_id not in evidence_ids:
                result['no_evidence'] += 1
                continue
            exists = conn.execute(
                '''SELECT id FROM relation_candidates
                   WHERE from_kp=? AND to_kp=? AND kind=?''',
                (left['id'], right['id'], kind),
            ).fetchone()
            edge_exists = conn.execute(
                'SELECT id FROM kp_edges WHERE from_kp=? AND to_kp=? AND kind=?',
                (left['id'], right['id'], kind),
            ).fetchone()
            if exists or edge_exists:
                result['duplicates'] += 1
                continue
            reason = ' '.join(str(item.get('reason') or '').split())[:800]
            cursor = conn.execute(
                '''INSERT INTO relation_candidates
                   (source_id,from_kp,to_kp,from_name,to_name,kind,evidence_id,
                    confidence,status,reason)
                   SELECT sf.id,?,?,?,?,?,?,0.65,'pending',?
                   FROM evidence ev JOIN source_files sf ON sf.id=ev.source_id
                   WHERE ev.id=? AND COALESCE(ev.stale,0)=0''',
                (left['id'], right['id'], from_name, to_name, kind, evidence_id,
                 reason or 'AI 跨章节关系草稿，需人工审核', evidence_id),
            )
            # The INSERT...SELECT can match no row when the evidence was retired
            # by a rescan; only count rows that really landed.
            result['created'] += max(0, cursor.rowcount)
        conn.commit()
        return result
    finally:
        conn.close()
