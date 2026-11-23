#!/usr/bin/env python3
"""Evaluate explicit offline JSON files; never discover sources or invoke models.

Exit codes: 0 complete, 2 valid but incomplete, 1 invalid input/output request.
Each input is at most 64 MiB, regular UTF-8 JSON, with depth at most 32.
The output parent must already exist; the output itself cannot be a symlink.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
from pathlib import Path
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.memory.extraction_eval import MAX_JSON_BYTES, MAX_JSON_DEPTH, evaluate, prepare

MAX_SOURCE_BYTES = 1024 * 1024 * 1024
MAX_SOURCE_COUNT = 10
MAX_TOTAL_SOURCE_BYTES = 2 * 1024 * 1024 * 1024
SOURCE_CHUNK_BYTES = 1024 * 1024


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default diagnostic echoes arbitrary argument values.
        raise ValueError("Invalid evaluation CLI arguments")


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def _invalid_constant(value):
    raise ValueError("Nonfinite JSON number")


def _integer(value):
    if len(value) > 128:
        raise ValueError("Oversized JSON integer")
    return int(value)


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Nonfinite JSON number")
    return number


def _load_json(path):
    # Nonblocking open plus fstat prevents named pipes/devices from hanging reads.
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_JSON_BYTES:
            raise ValueError("Input must be a bounded regular file")
        raw = stream.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError("Input exceeds byte limit")
    text = raw.decode("utf-8")
    depth, quoted, escaped = 0, False, False
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise ValueError("JSON exceeds depth limit")
        elif character in "]}":
            depth -= 1
    return json.loads(
        text, object_pairs_hook=_object, parse_constant=_invalid_constant, parse_int=_integer, parse_float=_float
    )


def _hash_source(path, *, max_bytes=MAX_SOURCE_BYTES):
    digest = hashlib.sha256()
    size = 0
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            raise ValueError("Source must be a bounded regular file")
        while chunk := stream.read(SOURCE_CHUNK_BYTES):
            size += len(chunk)
            if size > max_bytes:
                raise ValueError("Source exceeds byte limit")
            digest.update(chunk)
    return digest.hexdigest()


def _hash_sources(paths):
    if len(paths) > MAX_SOURCE_COUNT:
        raise ValueError("Too many source files")
    unique = sorted({str(Path(path).resolve(strict=True)) for path in paths})
    sources = []
    total_size = 0
    for path in unique:
        info = Path(path).stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SOURCE_BYTES:
            raise ValueError("Source must be a bounded regular file")
        total_size += info.st_size
        if total_size > MAX_TOTAL_SOURCE_BYTES:
            raise ValueError("Aggregate source size exceeds byte limit")
        sources.append((path, info.st_size))
    hashes = set()
    consumed = 0
    for path, declared_size in sources:
        remaining = MAX_TOTAL_SOURCE_BYTES - consumed
        digest = _hash_source(path, max_bytes=min(MAX_SOURCE_BYTES, remaining))
        size = Path(path).stat().st_size
        consumed += size
        if consumed > MAX_TOTAL_SOURCE_BYTES or size != declared_size:
            raise ValueError("Source set changed while hashing")
        hashes.add(digest)
    return sorted(hashes)


def _output_path(value, inputs):
    path = Path(os.path.abspath(value))
    if path.is_symlink():
        raise ValueError("Symlink output is not permitted")
    if path.exists() or not path.parent.is_dir():
        raise ValueError("Output must be a new directory with an existing parent")
    # A canonical parent allows normal macOS locations such as /tmp while still
    # detecting aliases through symlinks before creating the destination.
    resolved = path.parent.resolve() / path.name
    for source in inputs:
        source = Path(source).resolve()
        if resolved == source or resolved in source.parents:
            raise ValueError("Output aliases an input")
    return path


def _cell(value):
    text = html.escape(str(value), quote=True)
    for character in ("\\", "|", "`", "*", "_", "[", "]", "(", ")", "!"):
        text = text.replace(character, "\\" + character)
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def render_report(report):
    """Readable worksheet display; editable judgments stay in the fixed JSON rows."""
    counts = report["segments"]
    lines = [
        "# Offline extraction evaluation",
        "",
        f"Status: {report['status']}. Scope: {report['score_scope']}. Split: {report['split']}.",
        f"Holdout independence: {report['independence']}.",
        f"Source verification: {report['source_verification']['status']} ({report['source_verification']['verified_count']}/{report['source_verification']['expected_count']}).",
        f"Dataset: {_cell(report['dataset_id'])}",
        "",
        "Packet SHA256: " + report["packet_sha256"],
        "Responses SHA256: " + report["responses_sha256"],
        "Review SHA256: " + report["review_sha256"],
        "",
        "## Coverage",
        "",
        "| total | attempted | valid | missing | not run | error | provider error | timeout | invalid output |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        "| "
        + " | ".join(str(counts[key]) for key in ("total", "attempted", "valid", "missing", "not_run", "error", "provider_error", "timeout", "invalid_output"))
        + " |",
        "",
        "| segment | physical page | chapter | status |",
        "| --- | --- | --- | --- |",
    ]
    for row in report["segment_results"]:
        lines.append("| " + " | ".join(_cell(row[key]) for key in ("id", "page", "chapter", "status")) + " |")
    if report["segment_reviews"]:
        lines += ["", "## Annotation status", "", "| segment | status |", "| --- | --- |"]
        for row in report["segment_reviews"]:
            lines.append("| " + " | ".join(_cell(row[key]) for key in ("segment_id", "status")) + " |")
    lines += [
        "",
        "Structural validity: " + json.dumps(report["structural_validity"], sort_keys=True),
        "",
        "## Semantic scores",
        "",
        "Null means unavailable, including zero denominators.",
        "",
        "| item | precision | recall |",
        "| --- | --- | --- |",
    ]
    for category, scores in report["semantic_scores"].items():
        lines.append(f"| {category} | {json.dumps(scores['precision'])} | {json.dumps(scores['recall'])} |")
    lines += [
        "",
        "Incomplete reasons: " + (", ".join(report["incomplete_reasons"]) or "none"),
        "",
        "## Review worksheet",
        "",
        "Use the parsed predictions and gold below to fill review-template.json. "
        "Set reviewer and method explicitly. Mark supported true/false; "
        "use a gold id only for an explicit one-to-one match, otherwise null. "
        "Do not change binding hashes or add display fields to judgment rows.",
        "",
    ]
    for category, detail_key in (("nodes", "candidate"), ("relations", "relation")):
        lines += ["### Parsed " + category, ""]
        for number, prediction in enumerate(report["predictions"][category]):
            lines += [f"#### {category} item {number + 1}", ""]
            for key, value in prediction.items():
                if key != detail_key:
                    lines.append(f"- {key}: {_cell(value)}")
            for key, value in prediction[detail_key].items():
                display = ", ".join(value) if type(value) is list else value
                lines.append(f"- {key}: {_cell(display)}")
            lines.append("")
        if not report["predictions"][category]:
            lines += ["No parsed predictions.", ""]
        lines += ["### Gold " + category, ""]
        for row in report["gold"][category]:
            lines.append("- " + "; ".join(f"{key}: {_cell(value)}" for key, value in row.items()))
        if not report["gold"][category]:
            lines.append("No gold items.")
        lines.append("")
    lines += ["## Limits", ""] + ["- " + note for note in report["limitations"]]
    return "\n".join(lines) + "\n"


def main(argv=None):
    try:
        parser = _Parser(prog="evaluate_extraction.py", description=__doc__, allow_abbrev=False)
        for argument in ("packet", "responses", "output"):
            parser.add_argument("--" + argument, required=True)
        parser.add_argument("--review")
        parser.add_argument("--source", action="append", default=[])
        args = parser.parse_args(argv)
        inputs = [args.packet, args.responses, *args.source] + ([args.review] if args.review is not None else [])
        output = _output_path(args.output, inputs)
        packet, responses = _load_json(args.packet), _load_json(args.responses)
        verified_source_hashes = _hash_sources(args.source)
        report, template = prepare(packet, responses, verified_source_hashes=verified_source_hashes)
        validated_review = None
        if args.review is not None:
            validated_review = _load_json(args.review)
            report = evaluate(packet, responses, validated_review, verified_source_hashes=verified_source_hashes)
        files = {
            "report.json": json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            "report.md": render_report(report),
            "review-template.json": json.dumps(template, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        }
        if validated_review is not None:
            files["review.json"] = json.dumps(validated_review, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        output.mkdir(mode=0o700)
        for name, content in files.items():
            with (output / name).open("x", encoding="utf-8") as stream:
                stream.write(content)
    except (ValueError, OSError, TypeError, RecursionError, OverflowError):
        print("Evaluation failed: invalid input or unavailable new output directory.", file=sys.stderr)
        return 1
    print("Evaluation " + report["status"] + ".")
    return 0 if report["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
