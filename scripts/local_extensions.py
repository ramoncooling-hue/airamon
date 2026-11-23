"""Local-only administration; never exposed as an Agent tool or write API."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.integrations import mcp_bridge
from app.memory import retrieval_eval, repo, vector_index


def main():
    parser = argparse.ArgumentParser(description='MinDock 本机扩展管理（开发实验）')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status', help='读取配置状态，不启动 MCP 服务')
    probe = sub.add_parser('mcp-probe', help='启动已明确信任的服务，验证握手并列出允许的工具')
    probe.add_argument('--workspace', required=True)
    probe.add_argument('--course')
    probe.add_argument('--offline', action='store_true')
    for name in ('vector-build', 'vector-query'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--workspace', required=True)
        cmd.add_argument('--endpoint', required=True, help='例如 http://127.0.0.1:11434/v1/embeddings')
        cmd.add_argument('--model', required=True, help='已在本机部署的 Embedding 模型')
        if name == 'vector-query':
            cmd.add_argument('--query', required=True)
            cmd.add_argument('--course')
    evaluation = sub.add_parser('retrieval-eval', help='在选定数据目录比较全文和向量召回；建议使用测试副本')
    evaluation.add_argument('--workspace', required=True)
    evaluation.add_argument('--cases', type=Path, required=True)
    evaluation.add_argument('--endpoint', required=True)
    evaluation.add_argument('--model', required=True)
    evaluation.add_argument('--limit', type=int, default=6)
    args = parser.parse_args()
    try:
        if args.command == 'status':
            output = {'mcp': mcp_bridge.status(), 'vector': vector_index.status()}
        elif args.command == 'mcp-probe':
            result = mcp_bridge.discover(args.workspace, args.course, 'off' if args.offline else 'auto')
            output = {'tools': [t['function']['name'] for t in result.tools], 'errors': result.errors}
        elif args.command == 'retrieval-eval':
            embedder = vector_index.LocalEmbedding(args.endpoint, args.model)
            cases = retrieval_eval.load_cases(args.cases)
            keyword = lambda query, course: repo.search(query, args.limit, course, workspace=args.workspace)
            vector = lambda query, course: vector_index.search(query, args.workspace, embedder, course, args.limit)
            output = retrieval_eval.evaluate(cases, keyword, vector, args.limit)
            print(retrieval_eval.format_report(output))
            return 1 if any(s['status']=='incomplete' for s in output['summary'].values()) else 0
        else:
            embedder = vector_index.LocalEmbedding(args.endpoint, args.model)
            if args.command == 'vector-build':
                output = vector_index.rebuild(args.workspace, embedder)
            else:
                output = vector_index.search(args.query, args.workspace, embedder, args.course)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 1 if output.get('errors') or output.get('mcp', {}).get('error') else 0
    except Exception:
        # Do not dump HTTP responses, process arguments or material text.
        print('扩展操作失败。请检查可选依赖、本机配置、Embedding 服务及工作区；原始资料不受影响。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
