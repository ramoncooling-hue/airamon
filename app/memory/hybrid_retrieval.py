"""Experimental scoped candidate fusion; never an answerability classifier."""
import math

from . import search_index, vector_index


def _key(row):
    return (row.get('kind', 'kp'), row.get('id'))


def _fingerprint(row):
    return row.get('content_digest') or vector_index._digest(
        {k:v for k,v in row.items() if k != 'distance'})


def _keyword(query, workspace, course, limit):
    indexed = search_index.search(query, course, limit, workspace, material_only=True)
    current = {_key(r):r for r in vector_index._corpus(workspace, course)}
    result = []
    for row in indexed:
        key = ('file' if row['kind'] == 'source_chunk' else row['kind'], int(row['ref_id']))
        if key in current:
            result.append(current[key])
    return result


def fuse(query, keywords, vectors, current, *, min_similarity, limit=6):
    if type(min_similarity) not in (int, float) or not math.isfinite(min_similarity) or not 0 <= min_similarity <= 1:
        raise ValueError('An explicit finite similarity threshold in [0,1] is required')
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('limit must be in [1,50]')
    ready = isinstance(vectors, dict) and vectors.get('status') == 'ready' and not vectors.get('error')
    if ready and not isinstance(vectors.get('hits'), list):
        ready = False
    live = {_key(row):row for row in current}
    scores, similarities, exact = {}, {}, set()
    for rows, semantic in ((keywords, False), (vectors['hits'] if ready else [], True)):
        seen = set()
        for row in rows[:50]:
            key = _key(row)
            if key in seen or key not in live or _fingerprint(row) != vector_index._digest(live[key]):
                continue
            seen.add(key)
            if semantic:
                distance = row.get('distance')
                if type(distance) not in (int, float) or not math.isfinite(distance) or not 0 <= distance <= 2:
                    ready = False
                    continue
                similarities[key] = 1 - distance
            elif str(live[key]['name']).strip().casefold() == query.strip().casefold():
                exact.add(key)
            scores[key] = scores.get(key, 0) + 1 / (60 + len(seen))
    accepted = [key for key in scores if key in exact or similarities.get(key, -1) >= min_similarity]
    accepted.sort(key=lambda key: (-scores[key], str(key)))
    hits = []
    for key in accepted[:limit]:
        record = live[key]
        hits.append({k:v for k,v in record.items() if k != 'text'} | {
            'gist':record.get('text', '')[:1200], 'rrf_score':scores[key],
            'similarity':similarities.get(key), 'match':'exact_title' if key in exact else 'semantic_candidate'})
    return {'status':'ready' if ready else 'degraded', 'hits':hits,
            'decision': 'candidates' if hits else 'no_candidates' if ready else 'unavailable',
            'note':'候选相似不代表资料足以回答问题；需核对正文与来源。'}


def search(query, workspace, embedder, course=None, limit=6, *, min_similarity):
    keyword_ready = True
    try:
        keywords = _keyword(query, workspace, course, 50)
    except Exception:
        keywords, keyword_ready = [], False
    try:
        vectors = vector_index.search(query, workspace, embedder, course, 50)
    except Exception:
        vectors = {'status':'error'}
    try:
        current = vector_index._corpus(workspace, course)
    except Exception:
        current, keyword_ready, vectors = [], False, {'status':'error'}
    result = fuse(query, keywords, vectors, current, min_similarity=min_similarity, limit=limit)
    if not keyword_ready:
        result['status'] = 'degraded'
        if not result['hits']:
            result['decision'] = 'unavailable'
    return result
