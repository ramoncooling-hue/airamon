"""Run the public Chinese fixture in a separate temporary application database."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tests/fixtures/retrieval_zh.json'


def build_fixture():
    from app.memory import db, retrieval_eval, search_index
    fixture = json.loads(FIXTURE.read_text(encoding='utf-8'))
    db.apply_schema()
    conn = db.connect()
    cases = []
    try:
        workspace = conn.execute("SELECT id FROM workspaces WHERE key='studypal'").fetchone()[0]
        for number, document in enumerate(fixture['documents'], 1):
            conn.execute('INSERT OR IGNORE INTO courses(name,workspace_id) VALUES(?,?)', (document['course'], workspace))
            cid = conn.execute('SELECT id FROM courses WHERE name=?', (document['course'],)).fetchone()[0]
            conn.execute('INSERT INTO kp(course_id,name,gist) VALUES(?,?,?)', (cid, document['name'], document['gist']))
            label = {'kind':'kp', 'course_name':document['course'], 'name':document['name']}
            for category, query in [('exact', document['name']), ('paraphrase', document['paraphrase'])]:
                cases.append({'id':f'{category}-{number:02}', 'query':query, 'course':document['course'], 'expected':[label]})
        conn.execute("INSERT INTO courses(name,workspace_id) SELECT '隔离测试',id FROM workspaces WHERE key='lifepal'")
        conn.execute("INSERT INTO kp(course_id,name,gist) SELECT id,'私人旅行密语','这条记录不应从学习区召回' FROM courses WHERE name='隔离测试'")
        conn.commit()
    finally:
        conn.close()
    search_index.rebuild()
    return retrieval_eval.validate_cases(cases + fixture['extra_cases'])


def worker(mode, endpoint, model, limit, model_dir=None, cases_path=None, min_similarity=None):
    _validate_options(mode, endpoint, model, limit, model_dir, min_similarity)
    # Even a direct --worker invocation must never initialize the user's data root.
    with tempfile.TemporaryDirectory(prefix='mindock-retrieval-eval-') as directory:
        os.environ['CYBERPAL_DATA_DIR'] = directory
        return _evaluate_fixture(mode, endpoint, model, limit, model_dir, cases_path, min_similarity)


def _evaluate_fixture(mode, endpoint, model, limit, model_dir=None, cases_path=None, min_similarity=None):
    sys.path.insert(0, str(ROOT))
    from app.memory import hybrid_retrieval, repo, retrieval_eval, vector_index
    cases = build_fixture()
    if cases_path:
        cases = retrieval_eval.load_cases(cases_path)
    vector, build_status = None, 'not_run'
    hybrid = None
    details, timings = {}, {}
    if mode in {'compare', 'onnx', 'hybrid'}:
        started = time.perf_counter()
        try:
            if mode in {'onnx', 'hybrid'}:
                from scripts.local_embedding_eval import OfflineEmbedding
                embedder = OfflineEmbedding(model_dir)
                model, details = embedder.model, embedder.details
            else:
                embedder = vector_index.LocalEmbedding(endpoint, model)
            timings['load_seconds'] = round(time.perf_counter() - started, 6)
            started = time.perf_counter()
            vector_index.rebuild('studypal', embedder)
            timings['index_seconds'] = round(time.perf_counter() - started, 6)
            vector = lambda q,c: vector_index.search(q, 'studypal', embedder, c, limit)
            if mode == 'hybrid':
                hybrid = lambda q,c: hybrid_retrieval.search(
                    q, 'studypal', embedder, c, limit, min_similarity=min_similarity)
            build_status = 'ready'
        except Exception:
            vector = lambda q,c: {'status':'error'}
            if mode == 'hybrid':
                hybrid = lambda q,c: {'status':'degraded', 'decision':'unavailable', 'hits':[]}
            build_status = 'failed'
    report = retrieval_eval.evaluate(cases, lambda q,c: repo.search(q, limit, c, workspace='studypal'),
                                     vector, limit, hybrid_search=hybrid)
    report['metadata'] = {
        'fixture_sha256': hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
        'app_version': (ROOT / 'VERSION').read_text().strip(), 'mode': mode,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'corpus_documents': len(json.loads(FIXTURE.read_text(encoding='utf-8'))['documents']),
        'isolated': True, 'vector_build': build_status,
        'model': model if mode != 'keyword' else None, 'model_details': details, 'timings': timings,
        'cases_sha256': hashlib.sha256(Path(cases_path).read_bytes()).hexdigest() if cases_path else None,
        'hybrid': {'min_similarity':min_similarity, 'rrf_constant':60, 'candidate_limit':50,
                   'answerability_checked':False} if mode == 'hybrid' else None,
        'scope': 'Self-authored development fixture; not a held-out benchmark or answer-quality evaluation.'}
    report['by_category'] = {}
    for category in sorted({r['id'].split('-')[0] for r in report['cases']}):
        subset = [r for r in report['cases'] if r['id'] == category or r['id'].startswith(category + '-')]
        report['by_category'][category] = {}
        for method in report['summary']:
            ready = [r[method] for r in subset if r[method]['status'] == 'ready']
            positives = [r for r in ready if r['expected']]
            complete = len(ready) == len(subset)
            report['by_category'][category][method] = {
                'status': 'not_run' if report['summary'][method]['status'] == 'not_run' else 'complete' if complete else 'incomplete',
                'cases':len(ready), 'total':len(subset), 'positive_cases':len(positives),
                'positive_without_results':sum(r['returned'] == 0 for r in positives),
                'negative_cases':len(ready)-len(positives),
                'negative_with_results':sum(not r['expected'] and r['returned'] > 0 for r in ready),
                'recall_at_1':sum(r['recall_at_1'] for r in positives)/len(positives) if positives and complete else None,
                'recall_at_k':sum(r['recall'] for r in positives)/len(positives) if positives and complete else None,
                'mrr':sum(r['mrr'] for r in positives)/len(positives) if positives and complete else None,
                'hit_at_1':sum(r['first_rank']==1 for r in positives)/len(positives) if positives and complete else None}
    return report


def _validate_options(mode, endpoint, model, limit, model_dir, min_similarity):
    if mode not in {'keyword', 'compare', 'onnx', 'hybrid'} or (mode == 'compare' and (not endpoint or not model)):
        raise ValueError('compare 需要本机 endpoint 和 model')
    if mode in {'onnx', 'hybrid'} and model_dir is None:
        raise ValueError('onnx/hybrid 需要明确的本机模型目录')
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('limit 必须在 1 到 50 之间')
    if mode == 'hybrid' and (type(min_similarity) not in (int, float)
            or not math.isfinite(min_similarity) or not 0 <= min_similarity <= 1):
        raise ValueError('hybrid 必须明确设置 [0,1] 范围内的相似度门槛')


def run(mode='keyword', endpoint=None, model=None, limit=6, model_dir=None, cases_path=None, min_similarity=None):
    _validate_options(mode, endpoint, model, limit, model_dir, min_similarity)
    with tempfile.TemporaryDirectory(prefix='mindock-retrieval-eval-') as directory:
        env = os.environ.copy()
        env['CYBERPAL_DATA_DIR'] = directory
        command = [sys.executable, str(Path(__file__).resolve()), '--worker', '--mode', mode, '--limit', str(limit)]
        if endpoint:
            command += ['--endpoint', endpoint]
        if model:
            command += ['--model', model]
        if model_dir is not None:
            command += ['--model-dir', str(Path(model_dir).resolve())]
        if cases_path is not None:
            command += ['--cases', str(Path(cases_path).resolve())]
        if min_similarity is not None:
            command += ['--min-similarity', str(min_similarity)]
        completed = subprocess.run(command, env=env, cwd=ROOT, capture_output=True, text=True, timeout=1200)
        if completed.returncode:
            raise RuntimeError('隔离评测进程失败，未修改个人数据')
        return json.loads(completed.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['keyword', 'compare', 'onnx', 'hybrid'], default='keyword')
    parser.add_argument('--endpoint')
    parser.add_argument('--model')
    parser.add_argument('--model-dir', type=Path)
    parser.add_argument('--cases', type=Path, help='Alternative question set against the same synthetic corpus')
    parser.add_argument('--limit', type=int, default=6)
    parser.add_argument('--min-similarity', type=float, help='Required experimental threshold for hybrid mode')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(args.mode, args.endpoint, args.model, args.limit, args.model_dir, args.cases, args.min_similarity), ensure_ascii=False))
        return 0
    try:
        report = run(args.mode, args.endpoint, args.model, args.limit, args.model_dir, args.cases, args.min_similarity)
        if args.output:
            args.output.mkdir(parents=True, exist_ok=False)
            sys.path.insert(0, str(ROOT))
            from app.memory.retrieval_eval import format_report
            (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            (args.output / 'report.md').write_text(format_report(report), encoding='utf-8')
        print(json.dumps({'metadata':report['metadata'], 'summary':report['summary'], 'by_category':report['by_category']}, ensure_ascii=False, indent=2))
        return 1 if any(s['status']=='incomplete' for s in report['summary'].values()) else 0
    except Exception:
        print('评测未完成。检查参数、输出目录是否已存在、可选依赖与本机 Embedding 服务。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
