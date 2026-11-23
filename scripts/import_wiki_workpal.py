#!/usr/bin/env python3
"""CLI：把本机 Obsidian Wiki 图谱导入 WorkPal 知识库。

解析/建图逻辑现在位于 app/memory/wiki_import.py；本脚本只保留命令行与既有导出名
（旧测试与自动化依赖 parse_page/iter_pages/_wiki_links 等）。正式图写入通道由
导入模块使用（allow_api=False，按脚本名校验；测试运行器豁免）：
sources.authorize_formal_write('import_wiki_workpal')，随后 sources.record_formal_write(channel, ...)
写入 ingest_runs 审计行。默认只做盘点，传 --apply 才会写库；写库前会备份数据库。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.memory.wiki_import import (  # noqa: F401 - legacy CLI exports stay available
    DEFAULT_COURSE,
    DEFAULT_WIKI_ROOT,
    MAX_DETAIL,
    MAX_EVIDENCE_CHARS,
    MAX_GIST,
    SCHEMA_VERSION,
    WORKPAL_WORKSPACE,
    WikiPage,
    _backup_database,
    _blocks,
    _chapter_values,
    _check_course,
    _copy_path,
    _ensure_course,
    _frontmatter,
    _json,
    _merge_refs,
    _node_type,
    _page_name,
    _plain_excerpt,
    _resolve_link,
    _section,
    _status,
    _tags,
    _title,
    _wiki_links,
    import_pages,
    is_graph_page,
    iter_pages,
    parse_page,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="把本机 LLM Wiki 全量图谱导入 WorkPal")
    parser.add_argument("--wiki-root", type=Path, default=DEFAULT_WIKI_ROOT)
    parser.add_argument("--course", default=DEFAULT_COURSE)
    parser.add_argument("--apply", action="store_true", help="执行写入；默认只做盘点")
    args = parser.parse_args()
    result = import_pages(args.wiki_root, args.course, apply=args.apply, allow_api=False)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
