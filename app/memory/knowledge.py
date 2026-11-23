"""Canonical contract for knowledge extracted from local files.

The contract is intentionally independent from a model provider. Models may
propose a candidate, but only normalized candidates can enter ``kp``.
"""

from __future__ import annotations

from typing import Any

SCHEMA_VERSION = 'cyberpal.knowledge.v1'
NODE_TYPES = {
    'concept', 'model', 'method', 'case', 'exam_point', 'material',
    'entity', 'process', 'metric', 'event', 'decision', 'rule',
}
RELATION_KINDS = {
    'pre', 'related', 'part_of', 'supports', 'example_of', 'causes',
    'contrasts', 'bridge',
}
STATUSES = {
    'discovered', 'extracted', 'deduped', 'pending', 'pending_review',
    'confirmed', 'merged', 'rejected', 'stale',
}


def normalize_candidate(raw: dict[str, Any], *, source: str) -> dict[str, Any]:
    name = ' '.join(str(raw.get('name') or '').split())[:180]
    if not name:
        raise ValueError('知识点名称不能为空')
    kp_type = str(raw.get('kp_type') or 'material')
    if kp_type not in NODE_TYPES:
        kp_type = 'material'
    confidence = raw.get('confidence', 0.55)
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 0.55
    return {
        'schema_version': SCHEMA_VERSION,
        'name': name,
        'kp_type': kp_type,
        'chapter': str(raw.get('chapter') or '来源材料')[:180],
        'gist': str(raw.get('gist') or name)[:1000],
        'detail': str(raw.get('detail') or '')[:4000],
        'trap': str(raw.get('trap') or '')[:1000],
        'source': source,
        'confidence': confidence,
        'status': str(raw.get('status') or 'pending') if str(raw.get('status') or 'pending') in STATUSES else 'pending',
    }


def validate_relation(kind: str) -> str:
    if kind not in RELATION_KINDS:
        raise ValueError(f'不支持的知识关系：{kind}')
    return kind
