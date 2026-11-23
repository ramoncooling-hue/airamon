"""Curated, evidence-backed starter relations for the local WorkPal graph.

The imported Wiki notes are drafts, so this module deliberately seeds only a
small set of relations whose meaning is explicit in the note evidence. The
remaining model suggestions stay in ``relation_candidates`` for review.
"""

from __future__ import annotations

import json

from . import db
from . import domain_profiles
from . import repo


WORKPAL_RELATIONS = (
    {
        'from': 'PEV闭环', 'to': 'L1人机协作协议', 'kind': 'part_of',
        'evidence': 'PEV闭环(Plan-Execute-Verify)+工具调用+Skill沉淀',
        'reason': 'L1 人机协作协议的定义明确把 PEV 闭环列为支撑机制。', 'confidence': 0.94,
    },
    {
        'from': 'L1人机协作协议', 'to': '超级个体', 'kind': 'supports',
        'evidence': '三层协议框架——L1人机协作协议',
        'reason': 'Wiki 摘要将 L1 人机协作协议与超级个体公式放在同一条组织演化链上。', 'confidence': 0.90,
    },
    {
        'from': 'AI First动线', 'to': '超级个体', 'kind': 'part_of',
        'evidence': '超级个体的工作方式：先让AI跑',
        'reason': 'Wiki 对超级个体的定义明确包含 AI First 动线。', 'confidence': 0.95,
    },
    {
        'from': '超级个体', 'to': '超级个体到超级团队', 'kind': 'supports',
        'evidence': '超级个体如何聚合为超级团队',
        'reason': 'Wiki 明确把超级个体聚合为超级团队定义为核心命题。', 'confidence': 0.94,
    },
    {
        'from': '组织涌现', 'to': '超级个体到超级团队', 'kind': 'supports',
        'evidence': '个体觉醒→能力溢出→团队涌现→组织启示',
        'reason': 'Wiki 的涌现路径把个体、团队和组织连接成从下而上的演化链。', 'confidence': 0.90,
    },
    {
        'from': 'L2团队协作协议', 'to': '组织涌现', 'kind': 'supports',
        'evidence': '关键词不是"统一"而是"涌现"',
        'reason': 'L2 团队协作协议的定义明确以涌现作为关键协作方式。', 'confidence': 0.92,
    },
    {
        'from': '组织竞争力公式', 'to': '人才密度', 'kind': 'part_of',
        'evidence': '**人才密度**：单位团队中具备独立闭环能力的人占比',
        'reason': '人才密度是组织竞争力公式的分子因子。', 'confidence': 0.96,
    },
    {
        'from': '组织竞争力公式', 'to': 'AI杠杆', 'kind': 'part_of',
        'evidence': '**AI杠杆**：AI实际嵌入工作流的深度与覆盖面',
        'reason': 'AI 杠杆是组织竞争力公式的分子因子。', 'confidence': 0.96,
    },
    {
        'from': '组织竞争力公式', 'to': '组织摩擦', 'kind': 'part_of',
        'evidence': '**组织摩擦**：想法从产生到变成交付成果之间',
        'reason': '组织摩擦是组织竞争力公式的分母。', 'confidence': 0.96,
    },
    {
        'from': 'AI原生组织', 'to': '三层协议框架（L1/L2/L3）', 'kind': 'supports',
        'evidence': '三层协议框架——L1人机协作协议',
        'reason': '三层协议框架提供从人机、团队到组织的 AI 原生协作结构。', 'confidence': 0.88,
    },
    {
        'from': 'L3组织价值协议', 'to': '能力市场', 'kind': 'part_of',
        'evidence': '**能力市场**',
        'reason': 'L3 组织价值协议的定义明确以能力市场承载跨团队价值交换。', 'confidence': 0.93,
    },
    {
        'from': 'AI转型与AI原生统一框架', 'to': 'AI原生组织', 'kind': 'supports',
        'evidence': 'AI转型 = 存量组织内部',
        'reason': 'Wiki 将 AI 转型与 AI 原生放在同一框架，并以 AI 原生组织作为目标形态。', 'confidence': 0.86,
    },
    {
        'from': '个人护城河', 'to': '三大核心策略', 'kind': 'part_of',
        'evidence': '构建个人护城河',
        'reason': '个人护城河是三大核心策略中的第二项。', 'confidence': 0.95,
    },
    {
        'from': '复合型人才', 'to': '组织层面应对', 'kind': 'part_of',
        'evidence': '培养复合型人才',
        'reason': '培养复合型人才是材料列出的组织层面应对措施。', 'confidence': 0.95,
    },
)


def _evidence_id(conn, course_id: int, phrase: str) -> int | None:
    row = conn.execute(
        '''SELECT e.id FROM evidence e
           JOIN source_files sf ON sf.id=e.source_id
           WHERE sf.course_id=? AND instr(e.content,?)>0
             AND COALESCE(e.stale,0)=0
           ORDER BY e.id LIMIT 1''',
        (course_id, phrase),
    ).fetchone()
    return int(row['id']) if row else None


def seed(course: str = 'AI工作系统') -> dict:
    """Materialize curated starter edges and remain idempotent."""
    conn = db.connect()
    try:
        course_row = conn.execute('SELECT id FROM courses WHERE name=?', (course,)).fetchone()
        if not course_row:
            raise ValueError(f'课程不存在：{course}')
        course_id = int(course_row['id'])
        work_domain = domain_profiles.profile_for_course(course, 'WorkPal')['id']
        # Imported WorkPal notes may have been classified before the work
        # profile existed. Repair the contract in place without touching
        # evidence, aliases, or node content.
        for row in conn.execute('SELECT id,source FROM kp WHERE course_id=?', (course_id,)).fetchall():
            source_refs = json.dumps(
                [{'source': row['source'], 'quality': 'legacy'}]
                if str(row['source'] or '').strip() else [],
                ensure_ascii=False,
            )
            conn.execute(
                '''INSERT INTO knowledge_node_meta
                   (kp_id,schema_version,domain,source_refs_json,status)
                   VALUES(?,?,?,?, 'confirmed')
                   ON CONFLICT(kp_id) DO UPDATE SET domain=excluded.domain''',
                (row['id'], 'cyberpal.knowledge.v1', work_domain, source_refs),
            )
        conn.commit()
        nodes = {
            row['name']: int(row['id'])
            for row in conn.execute('SELECT id,name FROM kp WHERE course_id=?', (course_id,)).fetchall()
        }
        result = {'course': course, 'created': 0, 'updated': 0, 'skipped': []}
        for item in WORKPAL_RELATIONS:
            from_id, to_id = nodes.get(item['from']), nodes.get(item['to'])
            if not from_id or not to_id:
                result['skipped'].append({**item, 'reason': '节点不存在'})
                continue
            evidence_id = _evidence_id(conn, course_id, item['evidence'])
            if not evidence_id:
                result['skipped'].append({**item, 'reason': '未找到对应证据'})
                continue
            existing = conn.execute(
                'SELECT id FROM kp_edges WHERE from_kp=? AND to_kp=? AND kind=?',
                (from_id, to_id, item['kind']),
            ).fetchone()
            repo.create_edge(
                from_id, to_id, item['kind'], evidence_id=evidence_id,
                reason=item['reason'], confidence=item['confidence'],
            )
            result['updated' if existing else 'created'] += 1
        return result
    finally:
        conn.close()

