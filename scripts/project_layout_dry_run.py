#!/usr/bin/env python3
"""Report project-directory cleanup candidates without moving or deleting anything.

The report is intentionally conservative: runtime data, virtual environments,
and user-authored course files are always classified as protected.  A later
cleanup command may consume this report, but this script never performs file
operations beyond reading metadata and writing the report itself.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path


RULES = {
    '.git': ('protected', 'Git history and repository metadata'),
    '.venv': ('protected', 'local Python environment; recreate instead of sharing'),
    'data': ('protected', 'private database, settings, course files and user memory'),
    '_archive': ('retain', 'reversible historical archive; do not delete in place'),
    'output': ('review', 'process and delivery outputs; keep outside share packages'),
    'app': ('keep', 'backend source'),
    'web': ('keep', 'PC frontend and local runtime assets'),
    'agents': ('keep', 'Agent templates'),
    'skills': ('keep', 'Skill templates'),
    'domains': ('keep', 'domain contracts'),
    'seed': ('keep', 'public initialization data'),
    'scripts': ('keep', 'maintenance and packaging tools'),
    'tests': ('keep', 'automated verification'),
    'docs': ('keep', 'formal project documentation'),
}


def classify(path: Path, root: Path) -> tuple[str, str]:
    rel = path.relative_to(root)
    first = rel.parts[0] if rel.parts else ''
    if path.name == '.DS_Store' or path.suffix in {'.pyc', '.db-wal', '.db-shm'}:
        return 'generated', 'local OS/runtime artifact; safe to ignore and regenerate'
    if first in RULES:
        return RULES[first]
    if path.name in {'.gitignore', 'README.md', 'LICENSE', 'VERSION', 'CHANGELOG.md', 'requirements.txt', '启动伴学.command', 'start_cyberpal.bat'}:
        return 'keep', 'project entry or release metadata'
    return 'review', 'unclassified top-level project item; do not move automatically'


def scan(root: Path) -> dict:
    root = root.expanduser().resolve()
    rows = []
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            continue
        status, reason = classify(path, root)
        try:
            size = path.stat().st_size if path.is_file() else 0
        except OSError:
            size = 0
        rows.append({'path': str(path.relative_to(root)), 'kind': 'file' if path.is_file() else 'dir',
                     'bytes': size, 'status': status, 'reason': reason})
    protected = sorted({row['path'].split('/')[0] for row in rows if row['status'] == 'protected'})
    generated = [row['path'] for row in rows if row['status'] == 'generated']
    review = [row['path'] for row in rows if row['status'] == 'review']
    return {'root': str(root), 'read_only': True, 'generated_on': date.today().isoformat(),
            'summary': {'entries': len(rows), 'protected_roots': protected,
                        'generated_entries': len(generated), 'review_entries': len(review)},
            'entries': rows}


def markdown(report: dict) -> str:
    summary = report['summary']
    lines = [f"# 项目目录 dry-run 报告 · {report['generated_on']}", '',
             '> 本报告只读扫描，不移动、不删除、不归档任何文件。', '',
             f"- 根目录：`{report['root']}`", f"- 条目：{summary['entries']}",
             f"- 受保护根目录：{', '.join('`'+x+'`' for x in summary['protected_roots']) or '无'}",
             f"- 生成物：{summary['generated_entries']} 条", f"- 待人工确认：{summary['review_entries']} 条", '',
             '## 处理原则', '',
             '- `data/`、`.venv/`、`.git/` 只保留在本机，不进入分享包。',
             '- `output/` 和 `_archive/` 不自动删除；需要清理时先单独生成清单，再做可恢复归档。',
             '- `data/StudyPal/` 下的教材、课堂记录、通知照片和数据库不作为过程输出处理。',
             '- 未分类项目不自动移动。', '', '## 条目清单', '',
             '| 状态 | 类型 | 路径 | 说明 |', '|---|---|---|---|']
    for row in report['entries']:
        lines.append(f"| {row['status']} | {row['kind']} | `{row['path']}` | {row['reason']} |")
    return '\n'.join(lines) + '\n'


def main() -> int:
    parser = argparse.ArgumentParser(description='只读生成本机项目目录整理清单')
    parser.add_argument('--root', default=Path(__file__).resolve().parents[1], type=Path)
    parser.add_argument('--output', type=Path, help='Markdown 报告路径；默认写入 docs/')
    parser.add_argument('--json-output', type=Path, help='可选 JSON 报告路径')
    args = parser.parse_args()
    report = scan(args.root)
    output = args.output or args.root / 'docs' / f"PROJECT-LAYOUT-DRY-RUN-{report['generated_on']}.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(markdown(report), encoding='utf-8')
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'markdown': str(output), 'summary': report['summary']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
