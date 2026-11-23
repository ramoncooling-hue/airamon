"""Offline scoring only; retrieval callbacks own data access and boundaries."""
import json
import time
from collections import Counter
from pathlib import Path

MAX_CASES = 500


def _key(hit):
    return (str(hit.get('kind') or 'kp'), str(hit.get('course_name', '')), str(hit.get('name', '')))


def load_cases(path):
    path = Path(path)
    if path.stat().st_size > 1024 * 1024:
        raise ValueError('评测集过大')
    return validate_cases(json.loads(path.read_text(encoding='utf-8')))


def validate_cases(value):
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_CASES:
        raise ValueError('评测集必须包含 1 到 500 个问题')
    result, seen = [], set()
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get('query'), str):
            raise ValueError('评测项缺少 query')
        query = ' '.join(item['query'].split())
        expected = item.get('expected')
        course = item.get('course')
        identifier = item.get('id', str(len(result) + 1))
        if (not query or len(query) > 500 or not isinstance(expected, list) or len(expected) > 20
                or course is not None and (not isinstance(course, str) or not course.strip())
                or not isinstance(identifier, str) or not 1 <= len(identifier) <= 80 or identifier in seen):
            raise ValueError('评测项 query/course/id/expected 无效')
        seen.add(identifier)
        labels = []
        for label in expected:
            if (not isinstance(label, dict)
                    or not all(isinstance(label.get(key), str) and len(label[key]) <= 200
                               for key in ('kind', 'course_name', 'name'))
                    or not label['kind'] or not label['name']
                    or course is not None and label['course_name'] != course):
                raise ValueError('expected 必须包含范围内的 kind/course_name/name')
            labels.append({key: label[key] for key in ('kind', 'course_name', 'name')})
        result.append({'id': identifier, 'query': query, 'course': course, 'expected': labels})
    return result


def _metrics(rows, expected, limit):
    expected = {_key(item) for item in expected}
    keys = [_key(row) for row in rows[:limit]]
    found = len(set(keys) & expected)
    first = next((i + 1 for i, key in enumerate(keys) if key in expected), None)
    return {'status': 'ready', 'recall': found / len(expected) if expected else None,
            'recall_at_1': len(set(keys[:1]) & expected) / len(expected) if expected else None,
            'mrr': (1 / first if first else 0.0) if expected else None,
            'found': found, 'expected': len(expected),
            'first_rank': first, 'returned': len(keys),
            'retrieved': [dict(zip(('kind', 'course_name', 'name'), key)) for key in keys]}


def evaluate(cases, keyword_search, vector_search=None, limit=6, *, hybrid_search=None):
    cases = validate_cases(cases)
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('limit 必须是 1 到 50 的整数')
    methods = [('keyword', keyword_search), ('vector', vector_search), ('hybrid', hybrid_search)]
    reports = []
    for case in cases:
        row = {'id': case['id'], 'query': case['query'], 'course': case['course'],
               'expected': case['expected']}
        for name, search in methods:
            if search is None:
                row[name] = {'status': 'not_run'}
                continue
            started = time.perf_counter()
            row[name] = {}
            try:
                result = search(case['query'], case['course'])
                if isinstance(result, dict):
                    if isinstance(result.get('decision'), str):
                        row[name]['decision'] = result['decision']
                    status = result.get('status', 'ready')
                    if result.get('error') or status != 'ready':
                        row[name]['status'] = status if status in (
                            'not_built', 'rebuild_required', 'degraded', 'not_run') else 'error'
                        continue
                    result = result.get('hits')
                if not isinstance(result, list) or not all(isinstance(r, dict) for r in result):
                    raise ValueError('Invalid retrieval response')
                row[name].update(_metrics(result, case['expected'], limit))
            except Exception:
                row[name]['status'] = 'error'
            finally:
                row[name]['elapsed_seconds'] = round(time.perf_counter() - started, 6)
        reports.append(row)
    summary = {}
    for name, search in methods:
        ready = [row[name] for row in reports if row[name]['status'] == 'ready']
        positives = [r for r in ready if r['expected']]
        negatives = [r for r in ready if not r['expected']]
        negative_with_results = sum(r['returned'] > 0 for r in negatives)
        negative_without_results = sum(r['returned'] == 0 for r in negatives)
        complete = len(ready) == len(cases)
        summary[name] = {
            'status': 'not_run' if search is None else 'complete' if complete else 'incomplete',
            'evaluated': len(ready), 'total': len(cases), 'positive_cases': len(positives),
            'status_counts': dict(Counter(row[name]['status'] for row in reports)),
            'decision_counts': dict(Counter(row[name]['decision'] for row in reports if 'decision' in row[name])),
            'recall_at_1': sum(r['recall_at_1'] for r in positives) / len(positives) if complete and positives else None,
            'recall_at_k': sum(r['recall'] for r in positives) / len(positives) if complete and positives else None,
            'mrr': sum(r['mrr'] for r in positives) / len(positives) if complete and positives else None,
            'hit_at_1': sum(r['first_rank'] == 1 for r in positives) / len(positives) if complete and positives else None,
            'elapsed_seconds': round(sum(r[name].get('elapsed_seconds', 0) for r in reports), 6),
            'positive_without_results': sum(r['returned'] == 0 for r in positives),
            'negative_cases': len(negatives),
            'negative_with_results': negative_with_results,
            'negative_without_results': negative_without_results,
            'negative_with_results_rate': negative_with_results / len(negatives) if complete and negatives else None,
            'negative_without_results_rate': negative_without_results / len(negatives) if complete and negatives else None,
        }
    return {'limit': limit, 'cases': reports, 'summary': summary}


def _report_cells(metrics):
    evaluated = metrics.get('evaluated', metrics.get('cases', 0))
    total = metrics['total']
    status = metrics.get('status', 'complete' if evaluated == total else 'incomplete')
    complete = status == 'complete' and evaluated == total
    positives, negatives = metrics['positive_cases'], metrics['negative_cases']

    def value(number, count):
        if not complete:
            return '未评定'
        if not count:
            return '不适用'
        return f'{number:.3f}' if number is not None else '未评定'

    # Older category summaries contain counts only; derive rates only when complete.
    negative_with_results = metrics['negative_with_results']
    negative_without_results = metrics.get('negative_without_results', negatives - negative_with_results)
    with_rate = metrics.get('negative_with_results_rate', negative_with_results / negatives if negatives else None)
    without_rate = metrics.get('negative_without_results_rate', negative_without_results / negatives if negatives else None)
    cells = [status, f'{evaluated}/{total}', str(positives)]
    cells.extend(value(metrics.get(key), positives) for key in ('recall_at_1', 'recall_at_k', 'mrr', 'hit_at_1'))
    zero_positive = metrics.get('positive_without_results')
    cells += [str(zero_positive) if zero_positive is not None else '不适用' if complete and not positives else '未评定',
              f'{negative_with_results}/{negatives}', f'{negative_without_results}/{negatives}',
              value(with_rate, negatives), value(without_rate, negatives)]
    return cells


def format_report(result):
    columns = ['状态', '完成问题', '正例', 'Recall@1', 'Recall@K', 'MRR', '首条命中率',
               '正例零结果数', '负例有结果', '负例零结果', '负例有结果率', '负例零结果率']

    def table_row(cells):
        return '| ' + ' | '.join(cells) + ' |'

    lines = [f"# 本地召回评测 · Recall@1 / Recall@{result['limit']} / MRR", '',
             '仅评估资料候选召回，不评估生成答案或答案支持；RRF 仅用于排序，不是置信度。',
             '未执行、失败及 degraded 不计为零分或成功拒绝；未完整执行时汇总指标未评定，计数仅包含 ready 结果。',
             'Recall 与 MRR 仅统计正例；完整评测中没有对应正例或负例时，指标不适用。', '',
             table_row(['检索'] + columns + ['查询总秒数', '状态计数', 'decision 计数']),
             table_row(['---'] * (len(columns) + 4))]
    for name, summary in result['summary'].items():
        lines.append(table_row([name] + _report_cells(summary) + [
            f"{summary.get('elapsed_seconds', 0):.3f}",
            json.dumps(summary.get('status_counts', {}), ensure_ascii=False),
            json.dumps(summary.get('decision_counts', {}), ensure_ascii=False)]))
    if result.get('metadata'):
        lines += ['', '## 评测范围', '', '```json',
                  json.dumps(result['metadata'], ensure_ascii=False, indent=2), '```',
                  '', '使用自编开发语料；不代表真实教材覆盖率，也不代表回答质量。']
    if result.get('by_category'):
        lines += ['', '## 按问题类型', '', table_row(['类型', '检索'] + columns),
                  table_row(['---'] * (len(columns) + 2))]
        for category, methods in result['by_category'].items():
            for method, metrics in methods.items():
                lines.append(table_row([category, method] + _report_cells(metrics)))
    lines += ['', '## 逐题结果', '']
    for case in result['cases']:
        lines.append(f"{case['id']}: {json.dumps(case['query'], ensure_ascii=False)}")
        for name in result['summary']:
            metrics = case[name]
            state = metrics['status']
            if 'decision' in metrics:
                state += f"，decision={metrics['decision']}"
            if metrics['status'] != 'ready':
                lines.append(f"- {name}: {state}")
            else:
                labels = ' / '.join(f"{r['course_name']}:{r['name']}" for r in metrics['retrieved']) or '无结果'
                lines.append(f"- {name}: {state}；{metrics['found']}/{metrics['expected']}，首次命中 {metrics['first_rank'] or '-'}；{labels}")
        lines.append('')
    return '\n'.join(lines)
