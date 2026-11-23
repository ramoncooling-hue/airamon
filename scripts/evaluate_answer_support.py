"""Opt-in model evaluation using only the bundled synthetic evidence fixture."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tests/fixtures/answer_support_zh.json'
FIXTURES_DIR = ROOT / 'tests/fixtures'
REGISTERED_FIXTURES = {
    'answer_support_zh.json',
    'answer_support_holdout_zh.json',
    'answer_support_validation_zh.json',
}


def load_cases(path):
    """Load explicitly registered synthetic fixtures, never arbitrary user files."""
    candidate = Path(path).expanduser().resolve()
    fixtures_dir = FIXTURES_DIR.resolve()
    try:
        candidate.relative_to(fixtures_dir)
    except ValueError as exc:
        raise ValueError('case file must be inside tests/fixtures') from exc
    if candidate.parent != fixtures_dir or candidate.suffix.lower() != '.json':
        raise ValueError('case file must be a direct JSON child of tests/fixtures')
    if candidate.name not in REGISTERED_FIXTURES:
        raise ValueError('case file must be a registered synthetic fixture')
    cases = json.loads(candidate.read_text(encoding='utf-8'))
    if not isinstance(cases, list) or not 1 <= len(cases) <= 30:
        raise ValueError('invalid fixture size')
    from app.memory import answer_support
    identities = set()
    for case in cases:
        if (not isinstance(case, dict) or not isinstance(case.get('id'), str)
                or not case['id'] or case['id'] in identities
                or case.get('expected') not in answer_support.DECISIONS):
            raise ValueError('invalid fixture case')
        identities.add(case['id'])
        answer_support.build_packet(case['query'], case['documents'])
    return cases


def evaluate(cases, complete, progress=None):
    from app.memory import answer_support
    rows = []
    for case in cases:
        started = time.perf_counter()
        result = answer_support.assess(case['query'], case['documents'], complete)
        rows.append({'id':case['id'], 'expected':case['expected'], 'result':result,
                     'elapsed_seconds':round(time.perf_counter() - started, 3)})
        if progress:
            progress(len(rows), len(cases), result['status'])
    ready = [row for row in rows if row['result']['status'] == 'ready']
    model_evaluated = [row for row in ready
                       if row['result'].get('semantic_validation') == 'model_assessed']
    deterministic = [row for row in ready
                     if row['result'].get('semantic_validation') == 'not_run']
    attempted_rows = [row for row in rows if row['result'].get('model_attempted')]
    structured_responses = sum(row['result'].get('response_json_valid', False)
                               for row in attempted_rows)
    degraded = sum(row['result']['status'] == 'degraded' for row in attempted_rows)
    degraded_reasons = {}
    failure_codes = {}
    for row in attempted_rows:
        reason = row['result'].get('failure_stage')
        if reason:
            degraded_reasons[reason] = degraded_reasons.get(reason, 0) + 1
        code = row['result'].get('failure_code')
        if code:
            failure_codes[code] = failure_codes.get(code, 0) + 1
    model_attempted = len(attempted_rows)
    done = len(model_evaluated) == len(rows)
    expected_insufficient = [row for row in rows if row['expected'] == 'insufficient']
    negatives = [row for row in model_evaluated if row['expected'] == 'insufficient']
    false_support = sum(row['result']['decision'] == 'supported' for row in negatives)
    matches = sum(row['expected'] == row['result']['decision'] for row in model_evaluated)
    return {'cases':rows, 'summary':{
        'status':'complete' if done else 'incomplete', 'total':len(rows),
        'evaluated':len(model_evaluated),
        'model_attempted':model_attempted,
        'structured_responses':structured_responses,
        'structured_response_rate':structured_responses/model_attempted if model_attempted else None,
        'valid_judgments':len(model_evaluated),
        'valid_judgment_rate':len(model_evaluated)/model_attempted if model_attempted else None,
        'degraded':degraded,
        'degraded_reasons':degraded_reasons,
        'failure_codes':failure_codes,
        'valid_response_rate':len(model_evaluated)/len(rows) if rows else None,
        'deterministic_cases':len(deterministic),
        'decision_matches':matches,
        'evaluated_decision_accuracy':matches/len(model_evaluated) if model_evaluated else None,
        'decision_accuracy':sum(row['expected'] == row['result']['decision'] for row in model_evaluated)/len(rows) if done and rows else None,
        'insufficient_cases':len(expected_insufficient),
        'insufficient_evaluated':len(negatives),
        'insufficient_marked_supported':false_support,
        'false_support_rate':false_support/len(negatives) if done and negatives else None,
        'elapsed_seconds':round(sum(row['elapsed_seconds'] for row in rows), 3)}}


def render(report):
    lines = ['# 段落证据支持评测', '',
             '使用合成开发资料，模型做语义判定，宿主仅验证引句和来源。不是人工盲测或端到端回答验收。',
             '不含个人数据库、教材、聊天或凭据；异常不计为成功拒绝。', '',
             '```json', json.dumps(report['summary'], ensure_ascii=False, indent=2), '```', '',
             '| 题目 | 标注 | 模型判定 | 状态 | 秒 |', '| --- | --- | --- | --- | --- |']
    for row in report['cases']:
        lines.append(f"| {row['id']} | {row['expected']} | {row['result']['decision']} | {row['result']['status']} | {row['elapsed_seconds']} |")
    lines += ['', '## 元数据', '', '```json', json.dumps(report['metadata'], ensure_ascii=False, indent=2), '```']
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-check', action='store_true', help='Send bundled synthetic cases to the configured fast model')
    parser.add_argument('--cases', type=Path, default=FIXTURE,
                        help='Registered synthetic fixture under tests/fixtures (default: answer_support_zh.json)')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not args.model_check:
        print('未运行：需显式添加 --model-check；此操作仅发送内置合成资料。')
        return 1
    if args.output.exists():
        print('输出目录已存在，请指定新目录。')
        return 1
    sys.path.insert(0, str(ROOT))
    from app.agent import llm
    if not llm.available('fast'):
        print('未评定：fast 模型尚未配置。未创建或修改个人资料。')
        return 1
    cases = load_cases(args.cases)
    from app.memory import answer_support
    def complete(messages):
        return llm.chat('fast', messages, temperature=0,
                         response_format={'type': 'json_object'}, timeout_seconds=30)
    report = evaluate(cases, complete, progress=lambda n, total, status:
                      print(f'{n}/{total}: {status}', flush=True))
    report['metadata'] = {
        'generated_at':datetime.now(timezone.utc).isoformat(),
        'fixture_sha256':hashlib.sha256(args.cases.resolve().read_bytes()).hexdigest(),
        'protocol_sha256':hashlib.sha256(answer_support.INSTRUCTIONS.encode()).hexdigest(),
        'tier':'fast', 'response_format':{'type':'json_object'},
        'total_budget_seconds':30,
        'fixture_name':args.cases.name, 'app_version':(ROOT / 'VERSION').read_text().strip(),
        'personal_data_read':False, 'production_enabled':False,
        'scope':'Synthetic development fixture; model-assessed semantics, exact-quote host validation only.'}
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (args.output / 'report.md').write_text(render(report), encoding='utf-8')
    print(json.dumps(report['summary'], ensure_ascii=False, indent=2))
    return 0 if report['summary']['status'] == 'complete' else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print('评测未完成：请检查内置题集、输出目录和运行环境。', file=sys.stderr)
        raise SystemExit(1)
