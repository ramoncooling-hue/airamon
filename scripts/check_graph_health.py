#!/usr/bin/env python3
"""Read-only graph/mastery consistency report (B2/T2.13a).

Prints the structured report from graph_tools.check_graph_health() as JSON or
Markdown for W4 inspection records. It never repairs data and never runs
migrations: a missing or incompatible database is reported as unavailable.

Exit codes: 0 report produced, 1 --strict and errors were found, 2 database
unavailable/unreadable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ''}:  # pragma: no cover - direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.memory import db, graph_tools

EXIT_UNAVAILABLE = 2


def render_markdown(report: dict) -> str:
    lines = [
        f"# 图谱/掌握度一致性巡检 · {report.get('course', '')}",
        "",
        f"- 时间：{report.get('checked_at', '')}",
        f"- 结论：{'PASS' if report.get('ok') else 'FAIL'}",
        f"- 计数：{json.dumps(report.get('counts', {}), ensure_ascii=False)}",
        f"- errors / warnings：{report.get('totals', {}).get('errors', 0)} / "
        f"{report.get('totals', {}).get('warnings', 0)}",
    ]
    issues = report.get('issues') or []
    if issues:
        lines += ['', '| 类别 | 级别 | 数量 | 提示 |', '|---|---|---|---|']
        for item in issues:
            lines.append(f"| {item['kind']} | {item['severity']} | {item['count']} | {item.get('hint', '')} |")
    else:
        lines += ['', '未发现问题。']
    hints = report.get('hints') or []
    if hints:
        lines += ['', '## 降级/跳过', ''] + [f"- {hint}" for hint in hints]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='图谱/掌握度一致性巡检（只读）')
    parser.add_argument('--course', default='', help='只检查一门课程（默认全部课程）')
    parser.add_argument('--format', choices=('json', 'markdown'), default='json')
    parser.add_argument('--strict', action='store_true', help='发现 error 级问题时返回 1')
    args = parser.parse_args(argv)
    if not Path(db.DB_PATH).is_file():
        print(json.dumps({'ok': False, 'error': '数据库不存在或尚未初始化'}, ensure_ascii=False))
        return EXIT_UNAVAILABLE
    try:
        report = graph_tools.check_graph_health(args.course or None)
    except Exception as exc:  # noqa: BLE001 - CLI must report, never traceback on a bad DB
        print(json.dumps({'ok': False, 'error': str(exc)[:300]}, ensure_ascii=False))
        return EXIT_UNAVAILABLE
    if args.format == 'markdown':
        print(render_markdown(report))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if args.strict and not report.get('ok') else 0


if __name__ == '__main__':
    raise SystemExit(main())
