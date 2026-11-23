"""Synthetic, offline contracts; no semantic acceptance of real教材 is implied."""

import copy
import hashlib
import importlib
import json

import pytest


TEXT = "Alpha supports Beta. Gamma is an extra fact. PRIVATE_SOURCE_TAIL"
QUOTE = "Alpha supports Beta."


def evaluator():
    return importlib.import_module("app.memory.extraction_eval")


def packet(**changes):
    value = {
        "schema_version": 1,
        "dataset_id": "offline-demo",
        "dataset_kind": "synthetic",
        "split": "development",
        "used_for_tuning": False,
        "annotation_status": "human_reviewed",
        "annotator": "labeler",
        "mapping": {
            "branch_keys": ["work.industry_knowledge"],
            "node_types": ["concept"],
            "relation_kinds": ["supports", "contrasts"],
            "field_mappings": {},
        },
        "segments": [
            {
                "id": "s1",
                "chapter": "Chapter 1",
                "source_sha256": "a" * 64,
                "page": 1,
                "text": TEXT,
                "text_sha256": hashlib.sha256(TEXT.encode()).hexdigest(),
            }
        ],
        "gold_nodes": [
            {"id": "ga", "segment_id": "s1", "quote": "Alpha"},
            {"id": "gb", "segment_id": "s1", "quote": "Beta"},
        ],
        "gold_relations": [
            {"id": "gr", "segment_id": "s1", "from_id": "ga", "to_id": "gb", "kind": "supports", "quote": QUOTE}
        ],
    }
    value.update(changes)
    if value["dataset_kind"] == "real_textbook" and value["annotation_status"] == "human_reviewed":
        if "segment_reviews" not in changes:
            gold_segments = {
                row["segment_id"] for row in value["gold_nodes"] + value["gold_relations"]
            }
            value["segment_reviews"] = [
                {
                    "segment_id": segment["id"],
                    "status": "reviewed_with_gold" if segment["id"] in gold_segments else "reviewed_no_gold",
                }
                for segment in value["segments"]
            ]
    return value


def responses(*, extra=False, empty=False):
    nodes = [
        {
            "id": identifier,
            "name": name,
            "kp_type": "concept",
            "branch_keys": ["work.industry_knowledge"],
            "gist": name + " is a concept.",
            "detail": "",
            "chapter": "Chapter 1",
            "quote": quote,
        }
        for identifier, name, quote in [("a", "Alpha", "Alpha"), ("b", "Beta", "Beta")]
    ]
    if extra:
        nodes.append(dict(nodes[0], id="c", name="Gamma", gist="Extra fact.", quote="Gamma"))
    output = {
        "candidates": [] if empty else nodes,
        "relations": [] if empty else [{"from_id": "a", "to_id": "b", "kind": "supports", "quote": QUOTE}],
    }
    return [{"segment_id": "s1", "status": "ok", "output": json.dumps(output), "error_code": ""}]


def reviewed(data=None, result=None, *, method="synthetic", reviewer="reviewer"):
    data = packet() if data is None else data
    result = responses() if result is None else result
    _, review = evaluator().prepare(data, result)
    review.update(method=method, reviewer=reviewer)
    for row in review["nodes"]:
        row.update(supported=True, gold_id={"a": "ga", "b": "gb"}.get(row["candidate_id"]))
    for row in review["relations"]:
        row.update(supported=True, gold_id="gr")
    return review


def assert_suppressed(report):
    assert report["status"] == "incomplete"
    assert report["semantic_scores"] == {
        "nodes": {"precision": None, "recall": None},
        "relations": {"precision": None, "recall": None},
    }


def test_prepare_is_pure_and_separates_readable_display_from_fixed_judgments():
    data, result = packet(), responses()
    before = copy.deepcopy((data, result))
    report, review = evaluator().prepare(data, result)
    assert (data, result) == before
    assert_suppressed(report)
    assert review["method"] == "pending" and review["reviewer"] == ""
    assert review["nodes"] == [
        {"segment_id": "s1", "candidate_id": "a", "supported": None, "gold_id": None},
        {"segment_id": "s1", "candidate_id": "b", "supported": None, "gold_id": None},
    ]
    assert review["relations"] == [{"segment_id": "s1", "relation_index": 0, "supported": None, "gold_id": None}]
    assert report["predictions"]["nodes"][0]["candidate"]["gist"] == "Alpha is a concept."
    assert report["predictions"]["relations"][0]["relation"]["kind"] == "supports"
    serialized = json.dumps(report)
    assert TEXT not in serialized and "PRIVATE_SOURCE_TAIL" not in serialized
    assert "output" not in report and "text" not in report["segment_results"][0]
    assert len(review["packet_sha256"]) == 64 and len(review["responses_sha256"]) == 64


def test_complete_scores_count_extra_supported_facts_without_guessing_recall():
    result = responses(extra=True)
    review = reviewed(result=result)
    report = evaluator().evaluate(packet(), result, review)
    assert report["status"] == "complete" and report["score_scope"] == "synthetic"
    assert report["semantic_scores"]["nodes"] == {"precision": 1.0, "recall": 1.0}
    assert report["semantic_scores"]["relations"] == {"precision": 1.0, "recall": 1.0}
    review["nodes"][1]["gold_id"] = None
    review["relations"][0].update(supported=False, gold_id=None)
    report = evaluator().evaluate(packet(), result, review)
    assert report["semantic_scores"]["nodes"]["recall"] == 0.5
    assert report["semantic_scores"]["relations"] == {"precision": 0.0, "recall": 0.0}
    review["nodes"][2]["supported"] = False
    assert evaluator().evaluate(packet(), result, review)["semantic_scores"]["nodes"]["precision"] == pytest.approx(
        2 / 3
    )


def test_zero_denominators_are_null_even_when_complete():
    data = packet(gold_nodes=[], gold_relations=[])
    result = responses(empty=True)
    review = reviewed(data, result)
    report = evaluator().evaluate(data, result, review)
    assert report["status"] == "complete"
    assert report["semantic_scores"]["nodes"] == {"precision": None, "recall": None}
    assert report["semantic_scores"]["relations"] == {"precision": None, "recall": None}
    report = evaluator().evaluate(packet(), result, reviewed(packet(), result))
    assert report["semantic_scores"]["nodes"] == {"precision": None, "recall": 0.0}


def test_missing_failed_and_invalid_results_are_distinct_and_never_scored():
    data = packet(gold_nodes=[], gold_relations=[])
    base = data["segments"][0]
    data["segments"] = [dict(base, id=f"s{i}") for i in range(1, 5)]
    result = responses() + [
        {"segment_id": "s2", "status": "error", "output": "", "error_code": "timeout"},
        {"segment_id": "s3", "status": "ok", "output": "provider-invalid-output", "error_code": ""},
    ]
    report, review = evaluator().prepare(data, result)
    assert report["segments"] == {
        "total": 4,
        "attempted": 3,
        "valid": 1,
        "missing": 1,
        "not_run": 0,
        "error": 1,
        "provider_error": 0,
        "timeout": 1,
        "invalid_output": 1,
    }
    assert report["structural_validity"] == {"checked": 2, "valid": 1, "rate": 0.5}
    review.update(method="synthetic", reviewer="reviewer")
    for row in review["nodes"] + review["relations"]:
        row["supported"] = False
    assert_suppressed(evaluator().evaluate(data, result, review))
    assert "provider-invalid-output" not in json.dumps(report)


def test_not_run_is_not_attempted_and_failure_statuses_are_counted_separately():
    data = packet(gold_nodes=[], gold_relations=[])
    base = data["segments"][0]
    data["segments"] = [dict(base, id=f"s{i}") for i in range(1, 5)]
    result = [
        dict(responses(empty=True)[0], segment_id="s1"),
        {"segment_id": "s2", "status": "error", "output": "", "error_code": "provider_error"},
        {"segment_id": "s3", "status": "error", "output": "", "error_code": "timeout"},
        {"segment_id": "s4", "status": "error", "output": "", "error_code": "not_run"},
    ]

    report, _ = evaluator().prepare(data, result)

    assert report["segments"] == {
        "total": 4,
        "attempted": 3,
        "valid": 1,
        "missing": 0,
        "not_run": 1,
        "error": 2,
        "provider_error": 1,
        "timeout": 1,
        "invalid_output": 0,
    }
    assert [row["status"] for row in report["segment_results"]] == [
        "valid", "provider_error", "timeout", "not_run"
    ]


def test_real_human_review_requires_explicit_segment_annotation_status():
    data = packet(
        dataset_kind="real_textbook",
        gold_nodes=[],
        gold_relations=[],
        segment_reviews=[{"segment_id": "s1", "status": "reviewed_no_gold"}],
    )
    report = evaluator().evaluate(
        data,
        responses(empty=True),
        reviewed(data, responses(empty=True), method="human"),
        verified_source_hashes=["a" * 64],
    )
    assert report["status"] == "complete"
    assert report["segment_reviews"] == data["segment_reviews"]


def test_real_human_review_rejects_missing_segment_annotation_status():
    data = packet(dataset_kind="real_textbook", gold_nodes=[], gold_relations=[])
    data.pop("segment_reviews")
    with pytest.raises(ValueError, match="segment annotation"):
        evaluator().evaluate(
            data,
            responses(empty=True),
            reviewed(data, responses(empty=True), method="human"),
            verified_source_hashes=["a" * 64],
        )


def test_real_human_review_needs_ocr_stays_incomplete():
    data = packet(
        dataset_kind="real_textbook",
        gold_nodes=[],
        gold_relations=[],
        segment_reviews=[{"segment_id": "s1", "status": "needs_ocr"}],
    )
    report = evaluator().evaluate(
        data,
        responses(empty=True),
        reviewed(data, responses(empty=True), method="human"),
        verified_source_hashes=["a" * 64],
    )
    assert report["status"] == "incomplete"
    assert "segments_needs_ocr" in report["incomplete_reasons"]


def test_real_human_review_requires_gold_for_reviewed_with_gold():
    data = packet(
        dataset_kind="real_textbook",
        gold_nodes=[],
        gold_relations=[],
        segment_reviews=[{"segment_id": "s1", "status": "reviewed_with_gold"}],
    )
    with pytest.raises(ValueError, match="reviewed_with_gold"):
        evaluator().prepare(data, responses(empty=True))


def test_real_human_review_rejects_gold_on_reviewed_no_gold_segment():
    data = packet(
        dataset_kind="real_textbook",
        segment_reviews=[{"segment_id": "s1", "status": "reviewed_no_gold"}],
    )
    with pytest.raises(ValueError, match="reviewed_no_gold"):
        evaluator().prepare(data, responses())


def test_real_human_review_requires_exact_unique_segment_status_coverage():
    data = packet(dataset_kind="real_textbook")
    data["segments"].append(dict(data["segments"][0], id="s2"))
    data["segment_reviews"].append(dict(data["segment_reviews"][0]))
    with pytest.raises(ValueError, match="segment annotation"):
        evaluator().prepare(data, responses())


def test_real_draft_remains_compatible_without_segment_annotation_status():
    data = packet(dataset_kind="real_textbook", annotation_status="draft", gold_nodes=[], gold_relations=[])
    report = evaluator().evaluate(data, responses(empty=True))
    assert report["status"] == "incomplete"
    assert "draft_annotations" in report["incomplete_reasons"]


def test_invalid_output_envelope_counts_as_invalid_not_provider_error():
    data = packet(gold_nodes=[], gold_relations=[])
    result = [{"segment_id": "s1", "status": "error", "output": "", "error_code": "invalid_output"}]
    report, _ = evaluator().prepare(data, result)
    assert report["segments"] == {
        "total": 1, "attempted": 1, "valid": 0, "missing": 0, "not_run": 0,
        "error": 0, "provider_error": 0, "timeout": 0, "invalid_output": 1,
    }
    assert report["segment_results"][0]["error_code"] == "invalid_output"
    assert_suppressed(report)


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.update(method="pending"),
        lambda r: r.update(reviewer=""),
        lambda r: r["relations"].pop(),
    ],
)
def test_pending_or_partial_review_suppresses_all_scores(change):
    review = reviewed()
    change(review)
    assert_suppressed(evaluator().evaluate(packet(), responses(), review))


@pytest.mark.parametrize("missing", [False, True])
def test_missing_or_null_node_judgment_stays_incomplete(missing):
    review = reviewed()
    review["relations"][0].update(supported=False, gold_id=None)
    if missing:
        review["nodes"].pop()
    else:
        review["nodes"][0].update(supported=None, gold_id=None)
    assert_suppressed(evaluator().evaluate(packet(), responses(), review))


def test_draft_labels_always_suppress_scores():
    data = packet(annotation_status="draft")
    assert_suppressed(evaluator().evaluate(data, responses(), reviewed(data)))


@pytest.mark.parametrize(
    "changes,method,reviewer",
    [
        ({"annotator": ""}, "human", "reviewer"),
        ({}, "synthetic", "reviewer"),
        ({}, "human", " labeler "),
        ({}, "human", "LABELER"),
    ],
)
def test_real_scores_require_distinct_nonblank_human_declarations(changes, method, reviewer):
    data = packet(dataset_kind="real_textbook", **changes)
    assert_suppressed(evaluator().evaluate(data, responses(), reviewed(data, method=method, reviewer=reviewer)))


def test_real_complete_records_declaration_without_identity_proof():
    data = packet(dataset_kind="real_textbook", split="holdout")
    report = evaluator().evaluate(data, responses(), reviewed(data, method="human"), verified_source_hashes=["a" * 64])
    assert report["status"] == "complete" and report["score_scope"] == "real_textbook"
    assert report["review"]["identity_verified"] is False
    assert report["split"] == "holdout"
    assert report["independence"] == "unverified_declaration"
    assert report["claims"] == ["declared_holdout_unverified"]
    assert any("externally controlled, append-only registration" in note for note in report["limitations"])


def test_review_report_preserves_exact_validated_rows_and_canonical_hash():
    data, result, review = packet(), responses(), reviewed()
    report = evaluator().evaluate(data, result, review)
    expected = hashlib.sha256(
        json.dumps(review, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert report["review_sha256"] == expected
    assert report["judgments"] == {"nodes": review["nodes"], "relations": review["relations"]}
    reordered = dict(reversed(list(review.items())))
    assert evaluator().evaluate(data, result, reordered)["review_sha256"] == expected


def test_real_textbook_requires_all_source_hashes_even_with_complete_human_review():
    data = packet(dataset_kind="real_textbook")
    report = evaluator().evaluate(data, responses(), reviewed(data, method="human"))
    assert report["status"] == "incomplete"
    assert "source_unverified" in report["incomplete_reasons"]
    assert report["source_verification"] == {
        "required": True,
        "status": "unverified",
        "expected_count": 1,
        "verified_count": 0,
    }
    assert report["semantic_scores"]["nodes"] == {"precision": None, "recall": None}


def test_verified_source_hashes_are_reported_without_source_content():
    data = packet(dataset_kind="real_textbook")
    result = responses()
    report = evaluator().evaluate(data, result, reviewed(data, method="human"), verified_source_hashes=["a" * 64])
    assert report["status"] == "complete"
    assert report["source_verification"] == {
        "required": True,
        "status": "verified",
        "expected_count": 1,
        "verified_count": 1,
    }
    assert TEXT not in json.dumps(report)


def test_unexpected_verified_hash_rejects_as_source_mismatch():
    with pytest.raises(ValueError, match="[Ss]ource hash mismatch"):
        evaluator().prepare(packet(dataset_kind="real_textbook"), [], verified_source_hashes=["b" * 64])


def test_partial_unique_source_hash_coverage_is_explicit():
    data = packet(dataset_kind="real_textbook", gold_nodes=[], gold_relations=[])
    segment = dict(data["segments"][0], id="s2", source_sha256="b" * 64)
    data["segments"].append(segment)
    data["segment_reviews"].append({"segment_id": "s2", "status": "reviewed_no_gold"})
    report, _ = evaluator().prepare(data, [], verified_source_hashes=["a" * 64])
    assert report["source_verification"] == {
        "required": True,
        "status": "partial",
        "expected_count": 2,
        "verified_count": 1,
    }
    assert "source_unverified" in report["incomplete_reasons"]


def test_invalid_unhashable_segment_review_status_is_rejected():
    data = packet(dataset_kind="real_textbook")
    data["segment_reviews"][0]["status"] = []
    with pytest.raises(ValueError, match="segment annotation"):
        evaluator().prepare(data, [])


@pytest.mark.parametrize("field", ["packet_sha256", "responses_sha256"])
def test_stale_review_rejected(field):
    review = reviewed()
    review[field] = "0" * 64
    with pytest.raises(ValueError):
        evaluator().evaluate(packet(), responses(), review)


def test_canonical_bindings_ignore_object_key_order_but_bind_exact_text_and_output():
    data, result = packet(), responses()
    report, review = evaluator().prepare(data, result)
    reordered = {key: data[key] for key in reversed(data)}
    assert evaluator().prepare(reordered, result)[1] == review
    expected = hashlib.sha256(
        json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    assert report["packet_sha256"] == expected
    result[0]["output"] += " "
    with pytest.raises(ValueError):
        evaluator().evaluate(data, result, reviewed())


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r["nodes"].append(dict(r["nodes"][0])),
        lambda r: r["relations"].append(dict(r["relations"][0])),
        lambda r: r["nodes"][0].update(candidate_id="unknown"),
        lambda r: r["nodes"][0].update(segment_id="unknown"),
        lambda r: r["relations"][0].update(relation_index=True),
        lambda r: r["relations"][0].update(relation_index=1),
        lambda r: r["nodes"][0].update(supported=1),
        lambda r: r["nodes"][0].update(supported=False),
        lambda r: r["nodes"][0].update(gold_id="gb"),
        lambda r: r["nodes"][0].update(gold_id="unknown"),
        lambda r: r["nodes"][0].update(display="unapproved extra field"),
    ],
)
def test_unknown_duplicate_malformed_or_inconsistent_judgments_reject(change):
    review = reviewed()
    change(review)
    with pytest.raises(ValueError):
        evaluator().evaluate(packet(), responses(), review)


def test_supported_relation_needs_supported_endpoints_even_without_gold_match():
    review = reviewed()
    review["nodes"][0].update(supported=False, gold_id=None)
    review["relations"][0]["gold_id"] = None
    with pytest.raises(ValueError):
        evaluator().evaluate(packet(), responses(), review)


@pytest.mark.parametrize("changes", [{"from_id": "gb", "to_id": "ga"}, {"kind": "contrasts"}])
def test_relation_gold_match_preserves_direction_and_kind(changes):
    data = packet()
    data["gold_relations"][0].update(changes)
    with pytest.raises(ValueError):
        evaluator().evaluate(data, responses(), reviewed(data))


def test_gold_matches_must_be_local_to_the_prediction_segment():
    data = packet()
    data["segments"].append(dict(data["segments"][0], id="s2"))
    data["gold_nodes"][0]["segment_id"] = "s2"
    data["gold_relations"] = []
    review = reviewed(data)
    review["relations"][0].update(supported=False, gold_id=None)
    with pytest.raises(ValueError):
        evaluator().evaluate(data, responses(), review)


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.update(schema_version=True),
        lambda p: p.update(schema_version=2),
        lambda p: p.update(used_for_tuning=1),
        lambda p: p.update(split="holdout", used_for_tuning=True),
        lambda p: p.update(dataset_kind="benchmark"),
        lambda p: p.update(segments=[]),
        lambda p: p["segments"].append(dict(p["segments"][0])),
        lambda p: p["segments"][0].update(page=True),
        lambda p: p["segments"][0].update(page=0),
        lambda p: p["segments"][0].update(chapter=" "),
        lambda p: p["segments"][0].update(text=""),
        lambda p: p["segments"][0].update(text="x" * 4001),
        lambda p: p["segments"][0].update(text_sha256="0" * 64),
        lambda p: p["segments"][0].update(source_sha256="A" * 64),
        lambda p: p["gold_nodes"][0].update(quote="invented"),
        lambda p: p["gold_nodes"].append(dict(p["gold_nodes"][0])),
        lambda p: p["gold_relations"][0].update(kind="invented"),
        lambda p: p["gold_relations"][0].update(from_id="missing"),
        lambda p: p["mapping"].update(branch_keys=[]),
        lambda p: p.update(extra="not in contract"),
    ],
)
def test_invalid_packet_rejects_without_echoing_data(change):
    data = packet()
    change(data)
    with pytest.raises(ValueError) as exc:
        evaluator().prepare(data, responses())
    assert TEXT not in str(exc.value)


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.append(dict(r[0])),
        lambda r: r[0].update(segment_id="unknown"),
        lambda r: r[0].update(status="pending"),
        lambda r: r[0].update(error_code="timeout"),
        lambda r: r[0].update(status="error", output="", error_code="sk-PRIVATE"),
        lambda r: r[0].update(status="error", error_code="provider_error"),
        lambda r: r[0].update(output=None),
    ],
)
def test_invalid_response_envelopes_reject(change):
    result = responses()
    change(result)
    with pytest.raises(ValueError) as exc:
        evaluator().prepare(packet(), result)
    assert "sk-PRIVATE" not in str(exc.value)


def test_model_parser_is_reused_and_invalid_output_is_never_partially_salvaged(monkeypatch):
    from app.memory import extraction_output

    calls = []
    original = extraction_output.parse_output

    def tracked(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(extraction_output, "parse_output", tracked)
    report, _ = evaluator().prepare(packet(), responses())
    assert calls and report["segments"]["valid"] == 1
    result = responses()
    value = json.loads(result[0]["output"])
    value["relations"][0]["kind"] = "invented"
    result[0]["output"] = json.dumps(value)
    report, review = evaluator().prepare(packet(), result)
    assert report["segments"]["invalid_output"] == 1 and review["nodes"] == []


def test_real_names_collapse_internal_whitespace_as_declarations_only():
    data = packet(dataset_kind="real_textbook", annotator="Ada  Lovelace")
    review = reviewed(data, method="human", reviewer=" \tAda\nLovelace ")
    assert_suppressed(evaluator().evaluate(data, responses(), review))


def test_cross_segment_gold_relation_is_rejected_even_when_quotes_are_literal():
    data = packet()
    data["segments"].append(dict(data["segments"][0], id="s2"))
    data["gold_nodes"][1]["segment_id"] = "s2"
    with pytest.raises(ValueError):
        evaluator().prepare(data, responses())


def test_relation_gold_match_is_one_to_one():
    result = responses()
    payload = json.loads(result[0]["output"])
    payload["relations"].append(dict(payload["relations"][0]))
    result[0]["output"] = json.dumps(payload)
    with pytest.raises(ValueError):
        evaluator().evaluate(packet(), result, reviewed(result=result))


def test_missing_node_gold_matches_cannot_be_inferred_from_literal_text():
    review = reviewed()
    review["nodes"][0]["gold_id"] = None
    with pytest.raises(ValueError):
        evaluator().evaluate(packet(), responses(), review)
    review["relations"][0]["gold_id"] = None
    report = evaluator().evaluate(packet(), responses(), review)
    assert report["semantic_scores"]["nodes"] == {"precision": 1.0, "recall": 0.5}
    assert report["semantic_scores"]["relations"] == {"precision": 1.0, "recall": 0.0}


def test_segment_count_boundary_is_bounded_without_truncation():
    data = packet(gold_nodes=[], gold_relations=[])
    data["segments"] = [dict(data["segments"][0], id=f"s{i}") for i in range(100)]
    report, _ = evaluator().prepare(data, [])
    assert report["segments"]["missing"] == 100
    data["segments"].append(dict(data["segments"][0], id="overflow"))
    with pytest.raises(ValueError):
        evaluator().prepare(data, [])


def test_exact_unicode_and_whitespace_text_hash_is_verified():
    data = packet(gold_nodes=[], gold_relations=[])
    text = " \t中文\n证据\u00e9\n "
    data["segments"][0].update(text=text, text_sha256=hashlib.sha256(text.encode()).hexdigest())
    assert_suppressed(evaluator().evaluate(data, []))
    data["segments"][0]["text"] = text.strip()
    with pytest.raises(ValueError):
        evaluator().prepare(data, [])


def test_invalid_oversized_model_output_is_classified_without_echo_or_salvage():
    result = responses()
    result[0]["output"] += " " * 131072
    report, review = evaluator().prepare(packet(), result)
    assert report["segments"]["invalid_output"] == 1 and not review["nodes"]


def test_zero_predictions_against_nonempty_gold_is_zero_recall_not_perfect():
    result = responses(empty=True)
    report = evaluator().evaluate(packet(), result, reviewed(packet(), result))
    assert report["semantic_scores"]["relations"] == {"precision": None, "recall": 0.0}
