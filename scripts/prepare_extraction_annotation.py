#!/usr/bin/env python3
"""Prepare a bounded, offline worksheet for human extraction annotation.

The generated JSON remains an evaluation packet draft. It contains the frozen
segments and empty gold arrays so a reviewer can edit it and pass it to
``evaluate_extraction.py`` after completing the annotation. This script never
calls a model, scans directories, opens the runtime database, or writes source
paths into its output.
"""

from __future__ import annotations

import argparse
import html
import json
import os
from pathlib import Path
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.memory import extraction_eval


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("Invalid annotation CLI arguments")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Nonfinite JSON number")


def _load_json(path: str):
    candidate = Path(path)
    descriptor = os.open(candidate, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > extraction_eval.MAX_JSON_BYTES:
            raise ValueError("Input must be a bounded regular file")
        raw = stream.read(extraction_eval.MAX_JSON_BYTES + 1)
    if len(raw) > extraction_eval.MAX_JSON_BYTES:
        raise ValueError("Input exceeds byte limit")
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object,
            parse_constant=_invalid_constant,
        )
    except (UnicodeError, ValueError, RecursionError):
        raise ValueError("Invalid annotation packet") from None


def _output_path(value: str, packet_path: str) -> Path:
    output = Path(os.path.abspath(value))
    if output.is_symlink() or output.exists() or not output.parent.is_dir():
        raise ValueError("Output must be a new directory with an existing parent")
    source = Path(packet_path).resolve(strict=True)
    resolved = output.parent.resolve() / output.name
    if resolved == source or resolved in source.parents:
        raise ValueError("Output aliases an input")
    return output


def _escape(value) -> str:
    return html.escape(str(value), quote=True)


def _render_html(packet: dict, packet_sha256: str) -> str:
    sections = []
    for segment in packet["segments"]:
        sections.append(
            "<section>"
            f"<h2>PDF 第 {_escape(segment['page'])} 页 · {_escape(segment['chapter'])}</h2>"
            f"<p class=meta>片段 {_escape(segment['id'])} · "
            f"{len(segment['text'])} 字符 · text_sha256 {_escape(segment['text_sha256'])}</p>"
            "<p class=instruction>请只根据下方冻结原文填写标准知识声明和关系。引用必须是原文中的连续文字；关系要保留方向。"
            "当前页面是人工标注草稿，不显示模型准确率。</p>"
            f"<pre>{_escape(segment['text'])}</pre>"
            "</section>"
        )
    return """<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>知识抽取人工标注工作台</title>
<style>
body{margin:0;background:#eef2f7;color:#172033;font:16px/1.7 system-ui,-apple-system,sans-serif}
main{max-width:1100px;margin:0 auto;padding:32px 24px 72px}
h1{margin:0 0 10px;font-size:28px}
.notice{background:#fff7dd;border-left:5px solid #c98a00;padding:16px 18px;margin:20px 0 28px}
.meta{color:#51627a;font-size:14px;word-break:break-word}
section{background:#fff;border:1px solid #b9c6d8;border-radius:10px;margin:22px 0;padding:22px;box-shadow:0 2px 8px #17203310}
h2{font-size:20px;margin:0 0 4px}
.instruction{background:#edf7ff;border-left:4px solid #2b80c5;padding:10px 12px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f7f9fc;border:1px solid #d8e0eb;border-radius:6px;padding:16px;margin:16px 0 0;font:15px/1.8 ui-monospace,SFMono-Regular,Menlo,monospace}
code{background:#e8edf5;padding:2px 5px;border-radius:4px}
</style>
<main>
<h1>知识抽取人工标注工作台</h1>
<div class=notice><strong>当前是草稿标注。</strong> 本页只帮助核对冻结教材片段。它不调用模型、不写正式图谱，也不代表抽取准确率已经通过。完成后编辑同目录的 <code>annotation-template.json</code>，再用评测 CLI 生成可审计报告。</div>
<p>数据集：<code>DATASET</code></p>
<p>Packet SHA256：<code>PACKET_SHA256</code></p>
<p>片段数量：COUNT。请分别记录最小知识声明、逐字证据和有明确依据的有向关系；无明确关系时允许保持为空。</p>
SECTIONS
</main>
</html>
""".replace("DATASET", _escape(packet["dataset_id"])).replace(
        "PACKET_SHA256", packet_sha256
    ).replace("COUNT", str(len(packet["segments"]))).replace("SECTIONS", "\n".join(sections))


def _draft(packet: dict) -> dict:
    # Preserve the frozen input exactly while making the review state explicit.
    draft = json.loads(json.dumps(packet, ensure_ascii=False))
    draft["annotation_status"] = "draft"
    draft["annotator"] = ""
    draft["gold_nodes"] = []
    draft["gold_relations"] = []
    draft["segment_reviews"] = [
        {"segment_id": segment["id"], "status": "pending"} for segment in draft["segments"]
    ]
    return draft


def main(argv=None):
    try:
        parser = _Parser(prog="prepare_extraction_annotation.py", description=__doc__, allow_abbrev=False)
        parser.add_argument("--packet", required=True)
        parser.add_argument("--output", required=True)
        args = parser.parse_args(argv)
        output = _output_path(args.output, args.packet)
        packet = _load_json(args.packet)
        extraction_eval._validate_packet(packet)
        packet_sha256 = extraction_eval._canonical_sha(packet)
        draft = _draft(packet)
        files = {
            "annotation-template.json": json.dumps(draft, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            "annotation.html": _render_html(packet, packet_sha256),
            "README.md": (
                "# Extraction annotation worksheet\n\n"
                "This is a draft worksheet for human review. It does not call a model or publish a graph.\n\n"
                f"Packet SHA256: `{packet_sha256}`\n\n"
                "Fill `annotator`, `gold_nodes`, `gold_relations`, and each `segment_reviews` status in `annotation-template.json`. "
                "Then run `evaluate_extraction.py` with the original responses and this packet.\n"
            ),
        }
        output.mkdir(mode=0o700)
        for name, content in files.items():
            with (output / name).open("x", encoding="utf-8") as stream:
                stream.write(content)
    except (ValueError, OSError, TypeError, RecursionError, OverflowError):
        print("Annotation preparation failed: invalid input or unavailable new output directory.", file=sys.stderr)
        return 1
    print("Annotation worksheet prepared.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
