"""Bounded offline extraction evaluation. Imports never initialize runtime state.

Review records are declarations, not proof of identity or calibrated confidence.
Bindings use SHA256 of UTF-8, sorted-key, compact JSON (ensure_ascii=False).
Arrays, frozen source text and raw response strings retain their exact order/text.
Only parsed predictions and gold quotes appear in reports, never source segments
or unparsed provider output. No fuzzy or automatic semantic matching occurs.
"""

from __future__ import annotations

import hashlib
import json
import re

from app.memory import extraction_output


MAX_SEGMENTS = 100
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_JSON_DEPTH = 32
MAX_GOLD_NODES = MAX_SEGMENTS * extraction_output.MAX_CANDIDATES
MAX_GOLD_RELATIONS = MAX_SEGMENTS * extraction_output.MAX_RELATIONS
_EMPTY_OUTPUT = '{"candidates":[],"relations":[]}'
_PACKET_FIELDS = {
    "schema_version",
    "dataset_id",
    "dataset_kind",
    "split",
    "used_for_tuning",
    "annotation_status",
    "annotator",
    "mapping",
    "segments",
    "gold_nodes",
    "gold_relations",
}
_OPTIONAL_PACKET_FIELDS = {"segment_reviews"}
_SEGMENT_FIELDS = {"id", "chapter", "source_sha256", "page", "text", "text_sha256"}
_SEGMENT_REVIEW_FIELDS = {"segment_id", "status"}
_SEGMENT_REVIEW_STATUSES = {"pending", "reviewed_with_gold", "reviewed_no_gold", "needs_ocr"}
_REVIEW_FIELDS = {"reviewer", "method", "packet_sha256", "responses_sha256", "nodes", "relations"}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _fields(value, expected):
    _require(type(value) is dict and value.keys() == expected, "Invalid evaluation object fields")


def _text(value, *, limit=128, blank=False):
    _require(type(value) is str and len(value) <= limit, "Invalid bounded evaluation text")
    _require(blank or bool(value.strip()), "Evaluation text must be nonblank")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("Invalid evaluation Unicode") from None
    return value


def _array(value, maximum, minimum=0):
    _require(type(value) is list and minimum <= len(value) <= maximum, "Invalid evaluation array size")
    return value


def _sha(value):
    _require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None, "Invalid evaluation SHA256")
    return value


def _canonical_sha(value):
    # Check JSON types/depth before encoding; do not coerce keys, tuples or NaN.
    budget = 200000

    def visit(item, depth):
        nonlocal budget
        budget -= 1
        _require(budget >= 0 and depth <= MAX_JSON_DEPTH, "Evaluation JSON exceeds structural limits")
        if type(item) is dict:
            for key, child in item.items():
                _text(key, limit=MAX_JSON_BYTES, blank=True)
                visit(child, depth + 1)
        elif type(item) is list:
            for child in item:
                visit(child, depth + 1)
        elif type(item) is str:
            _text(item, limit=MAX_JSON_BYTES, blank=True)
        else:
            _require(item is None or type(item) in (bool, int, float), "Invalid evaluation JSON type")

    visit(value, 0)
    digest, size = hashlib.sha256(), 0
    try:
        for part in json.JSONEncoder(
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).iterencode(value):
            encoded = part.encode("utf-8")
            size += len(encoded)
            _require(size <= MAX_JSON_BYTES, "Evaluation JSON exceeds byte limit")
            digest.update(encoded)
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise ValueError("Invalid or oversized evaluation JSON") from None
    return digest.hexdigest()


def _validate_packet(packet):
    _require(
        type(packet) is dict
        and _PACKET_FIELDS <= packet.keys()
        and packet.keys() <= _PACKET_FIELDS | _OPTIONAL_PACKET_FIELDS,
        "Invalid evaluation packet fields",
    )
    _require(type(packet["schema_version"]) is int and packet["schema_version"] == 1, "Unsupported evaluation schema")
    _text(packet["dataset_id"])
    _require(packet["dataset_kind"] in ("synthetic", "real_textbook"), "Invalid dataset kind")
    _require(packet["split"] in ("development", "holdout"), "Invalid dataset split")
    _require(type(packet["used_for_tuning"]) is bool, "Invalid tuning declaration")
    _require(not (packet["split"] == "holdout" and packet["used_for_tuning"]), "A tuned holdout is not independent")
    _require(packet["annotation_status"] in ("draft", "human_reviewed"), "Invalid annotation status")
    _text(packet["annotator"], limit=256, blank=True)
    # Reuse the parser's mapping validation without importing any runtime layer.
    extraction_output.parse_output(_EMPTY_OUTPUT, mapping=packet["mapping"], evidence_text="")
    kinds = {" ".join(kind.split()) for kind in packet["mapping"]["relation_kinds"]}
    segments = {}
    for row in _array(packet["segments"], MAX_SEGMENTS, 1):
        _fields(row, _SEGMENT_FIELDS)
        identifier = _text(row["id"])
        _require(identifier not in segments, "Duplicate segment id")
        _text(row["chapter"], limit=256)
        _sha(row["source_sha256"])
        _require(type(row["page"]) is int and row["page"] > 0, "Invalid physical page number")
        text = _text(row["text"], limit=extraction_output.MAX_EVIDENCE_CHARS)
        _require(
            _sha(row["text_sha256"]) == hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "Frozen segment text hash mismatch",
        )
        segments[identifier] = row

    gold_nodes, gold_relations = {}, {}
    for field, maximum, index in (
        ("gold_nodes", MAX_GOLD_NODES, gold_nodes),
        ("gold_relations", MAX_GOLD_RELATIONS, gold_relations),
    ):
        fields = {"id", "segment_id", "quote"}
        if field == "gold_relations":
            fields = fields | {"from_id", "to_id", "kind"}
        for row in _array(packet[field], maximum):
            _fields(row, fields)
            identifier, segment = _text(row["id"]), _text(row["segment_id"])
            _require(identifier not in index and segment in segments, "Invalid gold identity or segment")
            quote = _text(row["quote"], limit=extraction_output.MAX_QUOTE_CHARS)
            _require(quote in segments[segment]["text"], "Gold quote is not literal evidence")
            if field == "gold_relations":
                source, target = _text(row["from_id"]), _text(row["to_id"])
                kind = _text(row["kind"], limit=extraction_output.MAX_SYMBOL_CHARS)
                _require(
                    source in gold_nodes and target in gold_nodes and source != target and kind in kinds,
                    "Invalid gold relation endpoints or kind",
                )
                _require(
                    gold_nodes[source]["segment_id"] == segment == gold_nodes[target]["segment_id"],
                    "Only within-segment gold relations are supported",
                )
            index[identifier] = row

    if packet["dataset_kind"] == "real_textbook" and packet["annotation_status"] == "human_reviewed":
        _require("segment_reviews" in packet, "Missing segment annotation status")
    if "segment_reviews" in packet:
        review_rows = _array(packet["segment_reviews"], MAX_SEGMENTS)
        segment_review_index = {}
        for row in review_rows:
            _fields(row, _SEGMENT_REVIEW_FIELDS)
            segment = _text(row["segment_id"])
            status = row["status"]
            _require(
                segment in segments and segment not in segment_review_index
                and isinstance(status, str) and status in _SEGMENT_REVIEW_STATUSES,
                "Invalid or duplicate segment annotation status",
            )
            segment_review_index[segment] = status
        _require(
            segment_review_index.keys() == segments.keys(),
            "Segment annotation status must cover every segment exactly once",
        )
        gold_by_segment = {segment: 0 for segment in segments}
        for row in packet["gold_nodes"]:
            gold_by_segment[row["segment_id"]] += 1
        for segment, status in segment_review_index.items():
            if status == "reviewed_with_gold":
                _require(gold_by_segment[segment] > 0, "reviewed_with_gold requires gold nodes")
            if status == "reviewed_no_gold":
                _require(gold_by_segment[segment] == 0, "reviewed_no_gold cannot contain gold nodes")
    return segments, gold_nodes, gold_relations


def _source_verification(packet, verified_source_hashes):
    supplied = [] if verified_source_hashes is None else verified_source_hashes
    _require(type(supplied) in (list, tuple, set, frozenset), "Verified source hashes must be a bounded collection")
    _require(len(supplied) <= MAX_SEGMENTS, "Too many verified source hashes")
    verified = {_sha(value) for value in supplied}
    expected = {row["source_sha256"] for row in packet["segments"]}
    _require(verified <= expected, "Source hash mismatch")
    required = packet["dataset_kind"] == "real_textbook"
    status = (
        "not_required"
        if not required
        else "verified"
        if expected <= verified
        else "partial"
        if verified
        else "unverified"
    )
    return {
        "required": required,
        "status": status,
        "expected_count": len(expected),
        "verified_count": len(expected & verified),
    }


def _prepare(packet, responses, verified_source_hashes=None):
    segments, _, _ = _validate_packet(packet)
    source_verification = _source_verification(packet, verified_source_hashes)
    supplied = {}
    for row in _array(responses, MAX_SEGMENTS):
        _fields(row, {"segment_id", "status", "output", "error_code"})
        segment = _text(row["segment_id"])
        _require(segment in segments and segment not in supplied, "Unknown or duplicate response segment")
        _require(row["status"] in ("ok", "error"), "Invalid response status")
        _text(row["output"], limit=MAX_JSON_BYTES, blank=True)
        _text(row["error_code"], limit=32, blank=True)
        if row["status"] == "ok":
            _require(row["error_code"] == "", "Successful response must have blank error code")
        else:
            _require(
                row["output"] == "" and row["error_code"] in (
                    "provider_error", "timeout", "not_run", "invalid_output"
                ),
                "Invalid failed response envelope",
            )
        supplied[segment] = row

    bindings = {"packet_sha256": _canonical_sha(packet), "responses_sha256": _canonical_sha(responses)}
    review = {"reviewer": "", "method": "pending", **bindings, "nodes": [], "relations": []}
    counts = dict(
        total=len(segments), attempted=0, valid=0, missing=0, not_run=0,
        error=0, provider_error=0, timeout=0, invalid_output=0,
    )
    predictions, results = {"nodes": [], "relations": []}, []
    for segment_id, segment in segments.items():
        result = {key: segment[key] for key in ("id", "chapter", "page", "source_sha256", "text_sha256")}
        response = supplied.get(segment_id)
        state = "missing" if response is None else "valid"
        if response is not None and response["status"] == "error":
            result["error_code"] = response["error_code"]
            state = response["error_code"]
            if state in ("provider_error", "timeout", "invalid_output"):
                counts["attempted"] += 1
            if state in ("provider_error", "timeout"):
                counts["error"] += 1
        if state == "valid":
            counts["attempted"] += 1
            try:
                parsed = extraction_output.parse_output(
                    response["output"], mapping=packet["mapping"], evidence_text=segment["text"]
                )
            except ValueError:
                state = "invalid_output"
            else:
                for candidate in parsed["candidates"]:
                    key = {"segment_id": segment_id, "candidate_id": candidate["id"]}
                    review["nodes"].append({**key, "supported": None, "gold_id": None})
                    predictions["nodes"].append({**key, "candidate": candidate})
                for number, relation in enumerate(parsed["relations"]):
                    key = {"segment_id": segment_id, "relation_index": number}
                    review["relations"].append({**key, "supported": None, "gold_id": None})
                    predictions["relations"].append({**key, "relation": relation})
        counts[state] += 1
        results.append({**result, "status": state})
    checked = counts["valid"] + counts["invalid_output"]
    report = {
        "schema_version": 1,
        **{
            key: packet[key]
            for key in ("dataset_id", "dataset_kind", "split", "used_for_tuning", "annotation_status", "annotator")
        },
        **bindings,
        "score_scope": packet["dataset_kind"],
        "independence": "unverified_declaration" if packet["split"] == "holdout" else "not_applicable",
        "claims": ["declared_holdout_unverified"] if packet["split"] == "holdout" else [],
        "source_verification": source_verification,
        "status": "incomplete",
        "segments": counts,
        "segment_reviews": [dict(row) for row in packet.get("segment_reviews", [])],
        "segment_results": results,
        "structural_validity": {"checked": checked, "valid": counts["valid"], "rate": _ratio(counts["valid"], checked)},
        "predictions": predictions,
        "gold": {
            "nodes": [dict(row) for row in packet["gold_nodes"]],
            "relations": [dict(row) for row in packet["gold_relations"]],
        },
        "limitations": [
            "Literal quote membership and structural validity do not establish semantic truth.",
            "Reviewer and annotator names record declarations; identity is not verified.",
            "Synthetic scores do not establish real textbook accuracy.",
            "Only within-segment extraction is evaluated; no cross-chapter acceptance.",
            "Holdout independence remains unverified; proof requires future externally controlled, append-only registration and a one-shot evaluation protocol.",
        ],
    }
    return report, review


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def _review(packet, report, template, review):
    _fields(review, _REVIEW_FIELDS)
    reviewer = _text(review["reviewer"], limit=256, blank=True)
    _require(review["method"] in ("pending", "human", "synthetic"), "Invalid review method")
    for binding in ("packet_sha256", "responses_sha256"):
        _require(_sha(review[binding]) == template[binding], "Stale evaluation review binding")
    judgments, complete = {}, True
    for category, key_field, maximum in (
        ("nodes", "candidate_id", MAX_GOLD_NODES),
        ("relations", "relation_index", MAX_GOLD_RELATIONS),
    ):
        expected = {(row["segment_id"], row[key_field]) for row in template[category]}
        gold = {row["id"]: row for row in report["gold"][category]}
        rows, matched = {}, set()
        for row in _array(review[category], maximum):
            _fields(row, {"segment_id", key_field, "supported", "gold_id"})
            segment = _text(row["segment_id"])
            if category == "nodes":
                identifier = _text(row[key_field])
            else:
                identifier = row[key_field]
                _require(type(identifier) is int and identifier >= 0, "Invalid relation judgment index")
            key = (segment, identifier)
            _require(key in expected and key not in rows, "Unknown or duplicate judgment")
            _require(row["supported"] is None or type(row["supported"]) is bool, "Invalid support judgment")
            gold_id = row["gold_id"]
            if gold_id is not None:
                _text(gold_id)
                _require(
                    row["supported"] is True and gold_id in gold and gold_id not in matched,
                    "Gold matches must be supported, known and one-to-one",
                )
                _require(gold[gold_id]["segment_id"] == segment, "Gold match must belong to this segment")
                matched.add(gold_id)
            rows[key] = row
        complete = complete and rows.keys() == expected and all(row["supported"] is not None for row in rows.values())
        judgments[category] = rows

    for prediction in report["predictions"]["relations"]:
        segment = prediction["segment_id"]
        row = judgments["relations"].get((segment, prediction["relation_index"]))
        if row is None or row["supported"] is not True:
            continue
        relation = prediction["relation"]
        source = judgments["nodes"].get((segment, relation["from_id"]))
        target = judgments["nodes"].get((segment, relation["to_id"]))
        _require(
            source is not None and target is not None and source["supported"] is True and target["supported"] is True,
            "Supported relations require supported endpoints",
        )
        if row["gold_id"] is not None:
            gold = next(item for item in packet["gold_relations"] if item["id"] == row["gold_id"])
            _require(
                (source["gold_id"], target["gold_id"], relation["kind"])
                == (gold["from_id"], gold["to_id"], gold["kind"]),
                "Relation gold match direction or kind mismatch",
            )

    complete = complete and bool(reviewer.strip()) and review["method"] != "pending"
    if packet["dataset_kind"] == "real_textbook":
        annotator = " ".join(packet["annotator"].split()).casefold()
        complete = complete and review["method"] == "human" and bool(annotator)
        complete = complete and annotator != " ".join(reviewer.split()).casefold()
    return judgments, bool(complete)


def _finish(packet, report, template, review):
    judgments, reviewed = _review(packet, report, template, review)
    reasons = []
    for key in ("missing", "not_run", "error", "invalid_output"):
        if report["segments"][key]:
            reasons.append("segments_" + key)
    if packet["dataset_kind"] == "real_textbook" and packet["annotation_status"] == "human_reviewed":
        annotation_statuses = [row["status"] for row in packet["segment_reviews"]]
        if "pending" in annotation_statuses:
            reasons.append("segment_annotations_incomplete")
        if "needs_ocr" in annotation_statuses:
            reasons.append("segments_needs_ocr")
    if packet["annotation_status"] != "human_reviewed":
        reasons.append("draft_annotations")
    if report["source_verification"]["required"] and report["source_verification"]["status"] != "verified":
        reasons.append("source_unverified")
    if not reviewed:
        reasons.append("review_incomplete")
    report["status"] = "incomplete" if reasons else "complete"
    report["incomplete_reasons"] = reasons
    report["review"] = {
        "reviewer": review["reviewer"],
        "method": review["method"],
        "complete": reviewed,
        "identity_verified": False,
    }
    report["review_sha256"] = _canonical_sha(review)
    report["judgments"] = {category: [dict(row) for row in review[category]] for category in ("nodes", "relations")}
    report["semantic_scores"] = {}
    for category in ("nodes", "relations"):
        rows = judgments[category].values()
        complete = report["status"] == "complete"
        report["semantic_scores"][category] = {
            "precision": _ratio(sum(row["supported"] is True for row in rows), len(report["predictions"][category]))
            if complete
            else None,
            "recall": _ratio(sum(row["gold_id"] is not None for row in rows), len(report["gold"][category]))
            if complete
            else None,
        }
    return report


def prepare(packet, responses, verified_source_hashes=None):
    """Validate frozen inputs and return (pending report, exact review template)."""
    report, template = _prepare(packet, responses, verified_source_hashes)
    return _finish(packet, report, template, template), template


def evaluate(packet, responses, review=None, verified_source_hashes=None):
    """Return a report; invalid/stale judgments raise sanitized ValueError.

    Missing rows and null judgments are valid unfinished work and suppress all
    semantic scores. Unknown, duplicate or inconsistent judgments are invalid.
    """
    report, template = _prepare(packet, responses, verified_source_hashes)
    return _finish(packet, report, template, template if review is None else review)
