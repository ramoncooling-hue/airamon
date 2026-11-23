#!/usr/bin/env python3
"""Run the approved extraction contract against an explicit packet.

This is an evaluation runner, not a graph importer. It calls the configured
model only when explicitly invoked, records a bounded response envelope, and
never writes the production database or formal graph.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent import llm
from app.memory import extraction_eval, extraction_output

MAX_TIMEOUT = 90
MAX_TIER = {"fast", "reasoning"}
PROVIDER_ERROR_CODES = (
    "http", "network", "json", "invalid_message", "model_error", "cancelled",
)
PROVIDER_HTTP_STATUS_BUCKETS = ("http_4xx", "http_5xx", "http_unknown")


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("Invalid extraction model CLI arguments")


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
            raw.decode("utf-8"), object_pairs_hook=_object,
            parse_constant=_invalid_constant,
        )
    except (UnicodeError, ValueError, RecursionError):
        raise ValueError("Invalid extraction packet") from None


def _output_path(value: str, packet_path: str) -> Path:
    output = Path(os.path.abspath(value))
    if output.is_symlink() or output.exists() or not output.parent.is_dir():
        raise ValueError("Output must be a new directory with an existing parent")
    source = Path(packet_path).resolve(strict=True)
    resolved = output.parent.resolve() / output.name
    if resolved == source or resolved in source.parents:
        raise ValueError("Output aliases an input")
    return output


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _error_details(exc: BaseException) -> tuple[str, str, str]:
    code = getattr(exc, "code", "")
    if code == "timeout" or isinstance(exc, TimeoutError):
        return "timeout", "", "http_unknown"
    provider_code = code if code in PROVIDER_ERROR_CODES else "model_error"
    bucket = getattr(exc, "http_status_bucket", "http_unknown")
    if bucket not in PROVIDER_HTTP_STATUS_BUCKETS:
        bucket = "http_unknown"
    return "provider_error", provider_code, bucket


def _response(segment_id: str, *, output: str = "", error_code: str = "") -> dict:
    if output:
        return {"segment_id": segment_id, "status": "ok", "output": output, "error_code": ""}
    return {"segment_id": segment_id, "status": "error", "output": "", "error_code": error_code}


def _run(packet: dict, *, tier: str, timeout: int, limit: int) -> tuple[list[dict], dict, dict, dict, list[dict]]:
    segments = packet["segments"]
    responses = []
    response_diagnostics = []
    counts = {
        "total": len(segments), "attempted": 0, "ok": 0,
        "provider_error": 0, "timeout": 0, "invalid_output": 0, "not_run": 0,
    }
    provider_error_codes = {code: 0 for code in PROVIDER_ERROR_CODES}
    provider_http_statuses = {bucket: 0 for bucket in PROVIDER_HTTP_STATUS_BUCKETS}
    for index, segment in enumerate(segments):
        if index >= limit:
            row = _response(segment["id"], error_code="not_run")
            counts["not_run"] += 1
            responses.append(row)
            continue
        counts["attempted"] += 1
        try:
            messages = extraction_output.build_messages(
                mapping=packet["mapping"], evidence_text=segment["text"]
            )
            raw = llm.chat(tier, messages, timeout_seconds=timeout, max_tokens=4000)
        except Exception as exc:
            public_code, provider_code, status_bucket = _error_details(exc)
            counts[public_code] += 1
            if public_code == "provider_error":
                provider_error_codes[provider_code] += 1
                provider_http_statuses[status_bucket] += 1
                if provider_code == "invalid_message":
                    metadata = llm.safe_response_metadata(getattr(exc, "response_metadata", None))
                    if metadata:
                        response_diagnostics.append(metadata)
            responses.append(_response(segment["id"], error_code=public_code))
            continue
        try:
            if type(raw) is not str:
                raise ValueError("Model output must be text")
            extraction_output.parse_output(
                raw, mapping=packet["mapping"], evidence_text=segment["text"]
            )
        except (ValueError, TypeError):  # Contract-invalid model output only; internal errors escape.
            counts["invalid_output"] += 1
            responses.append(_response(segment["id"], error_code="invalid_output"))
            continue
        counts["ok"] += 1
        responses.append(_response(segment["id"], output=raw))
    return responses, counts, provider_error_codes, provider_http_statuses, response_diagnostics


def main(argv=None):
    try:
        parser = _Parser(prog="run_extraction_model.py", description=__doc__, allow_abbrev=False)
        parser.add_argument("--packet", required=True)
        parser.add_argument("--output", required=True)
        parser.add_argument("--tier", choices=sorted(MAX_TIER), default="fast")
        parser.add_argument("--timeout", type=int, default=45)
        parser.add_argument("--limit", type=int)
        args = parser.parse_args(argv)
        if not 1 <= args.timeout <= MAX_TIMEOUT:
            raise ValueError("Invalid timeout")
        output = _output_path(args.output, args.packet)
        packet = _load_json(args.packet)
        extraction_eval._validate_packet(packet)
        limit = len(packet["segments"]) if args.limit is None else args.limit
        if not 1 <= limit <= len(packet["segments"]):
            raise ValueError("Invalid limit")
        started = _utc_now()
        responses, counts, provider_error_codes, provider_http_statuses, response_diagnostics = _run(
            packet, tier=args.tier, timeout=args.timeout, limit=limit
        )
        finished = _utc_now()
        run = {
            "schema_version": 1,
            "dataset_id": packet["dataset_id"],
            "packet_sha256": extraction_eval._canonical_sha(packet),
            "tier": args.tier,
            "limit": limit,
            "segments": counts,
            "provider_error_codes": provider_error_codes,
            "provider_http_statuses": provider_http_statuses,
            "response_diagnostics": response_diagnostics,
            "started_at": started,
            "finished_at": finished,
            "status": "complete" if counts["not_run"] == 0 and counts["ok"] == counts["total"] else "incomplete",
        }
        files = {
            "responses.json": json.dumps(responses, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            "run.json": json.dumps(run, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            "README.md": (
                "# Extraction model run\n\n"
                "This directory contains an explicit evaluation run. It does not publish a graph or modify the runtime database.\n\n"
                f"Dataset: `{packet['dataset_id']}`\n\n"
                f"Packet SHA256: `{run['packet_sha256']}`\n\n"
                f"Status: `{run['status']}`. Use `responses.json` with `evaluate_extraction.py`.\n"
            ),
        }
        output.mkdir(mode=0o700)
        for name, content in files.items():
            with (output / name).open("x", encoding="utf-8") as stream:
                stream.write(content)
    except (ValueError, OSError, TypeError, RecursionError, OverflowError):
        print("Extraction model run failed: invalid input or unavailable new output directory.", file=sys.stderr)
        return 1
    except Exception:
        print("Extraction model run failed: runner_error.", file=sys.stderr)
        return 1
    print("Extraction model run " + run["status"] + ".")
    return 0 if run["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
