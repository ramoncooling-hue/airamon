import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def packet():
    import hashlib
    segments = []
    for index in range(2):
        text = f"Evidence {index} supports concept {index}."
        segments.append({
            "id": f"s{index}", "chapter": "Synthetic", "source_sha256": "a" * 64,
            "page": index + 1, "text": text,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        })
    return {
        "schema_version": 1, "dataset_id": "synthetic-model-run", "dataset_kind": "synthetic",
        "split": "development", "used_for_tuning": False, "annotation_status": "draft",
        "annotator": "", "mapping": {"branch_keys": ["study.knowledge_understanding"],
        "node_types": ["concept"], "relation_kinds": ["supports"], "field_mappings": {}},
        "segments": segments, "gold_nodes": [], "gold_relations": [],
    }


def invoke(tmp_path, monkeypatch, *, limit=None, fake=None, extra=()):
    import scripts.run_extraction_model as runner
    packet_path = tmp_path / "packet.json"
    packet_path.write_text(json.dumps(packet(), ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "run"
    monkeypatch.setattr(runner.llm, "chat", fake or (lambda *args, **kwargs: json.dumps({"candidates": [], "relations": []})))
    args = ["--packet", str(packet_path), "--output", str(output)]
    if limit is not None:
        args += ["--limit", str(limit)]
    args += list(extra)
    result = runner.main(args)
    return result, output


def test_runs_strict_empty_results_and_binds_packet(monkeypatch, tmp_path):
    result, output = invoke(tmp_path, monkeypatch)
    assert result == 0
    responses = json.loads((output / "responses.json").read_text())
    assert len(responses) == 2 and all(row["status"] == "ok" for row in responses)
    run = json.loads((output / "run.json").read_text())
    assert run["status"] == "complete" and len(run["packet_sha256"]) == 64
    assert "output" not in run and "api_key" not in (output / "run.json").read_text()


def test_limit_marks_remaining_segments_not_run(monkeypatch, tmp_path):
    result, output = invoke(tmp_path, monkeypatch, limit=1)
    assert result == 2
    responses = json.loads((output / "responses.json").read_text())
    assert [row["error_code"] for row in responses] == ["", "not_run"]
    assert json.loads((output / "run.json").read_text())["status"] == "incomplete"


def test_provider_error_is_sanitized_and_not_saved(monkeypatch, tmp_path):
    secret = "provider secret /private/source.pdf"
    def fail(*args, **kwargs):
        raise RuntimeError(secret)
    result, output = invoke(tmp_path, monkeypatch, fake=fail)
    assert result == 2
    text = "\n".join(path.read_text() for path in output.iterdir())
    assert secret not in text
    responses = json.loads((output / "responses.json").read_text())
    assert all(row["error_code"] == "provider_error" for row in responses)
    assert all(set(row) == {"segment_id", "status", "output", "error_code"} for row in responses)


def test_run_counts_safe_provider_error_codes_and_http_status_buckets(monkeypatch, tmp_path):
    from app.agent.llm import LLMError
    failures = iter([
        LLMError("secret response body", code="http", http_status_bucket="http_4xx"),
        LLMError("private URL and body", code="network", http_status_bucket="http_5xx"),
    ])
    result, output = invoke(
        tmp_path, monkeypatch,
        fake=lambda *args, **kwargs: (_ for _ in ()).throw(next(failures)),
    )
    assert result == 2
    run = json.loads((output / "run.json").read_text())
    assert run["provider_error_codes"] == {
        "http": 1, "network": 1, "json": 0, "invalid_message": 0,
        "model_error": 0, "cancelled": 0,
    }
    assert run["provider_http_statuses"] == {
        "http_4xx": 1, "http_5xx": 1, "http_unknown": 0,
    }
    responses = json.loads((output / "responses.json").read_text())
    assert all(row["error_code"] == "provider_error" for row in responses)
    assert all(set(row) == {"segment_id", "status", "output", "error_code"} for row in responses)
    saved = "\n".join(path.read_text() for path in output.iterdir())
    assert "secret response body" not in saved and "private URL and body" not in saved


@pytest.mark.parametrize("provider_code", [
    "http", "network", "json", "invalid_message", "model_error", "cancelled",
])
def test_run_counts_each_allowed_llm_error_code(monkeypatch, tmp_path, provider_code):
    from app.agent.llm import LLMError
    error = LLMError("private provider detail", code=provider_code)
    result, output = invoke(tmp_path, monkeypatch, fake=lambda *args, **kwargs: (_ for _ in ()).throw(error))
    assert result == 2
    run = json.loads((output / "run.json").read_text())
    assert run["provider_error_codes"][provider_code] == 2
    assert sum(run["provider_error_codes"].values()) == 2
    assert all(
        row["error_code"] == "provider_error"
        for row in json.loads((output / "responses.json").read_text())
    )


def test_unknown_llm_error_code_and_http_bucket_are_normalized(monkeypatch, tmp_path):
    from app.agent.llm import LLMError
    error = LLMError("private provider content", code="vendor_secret", http_status_bucket="secret")
    result, output = invoke(tmp_path, monkeypatch, fake=lambda *args, **kwargs: (_ for _ in ()).throw(error))
    assert result == 2
    run = json.loads((output / "run.json").read_text())
    assert run["provider_error_codes"]["model_error"] == 2
    assert run["provider_http_statuses"] == {
        "http_4xx": 0, "http_5xx": 0, "http_unknown": 2,
    }
    assert "vendor_secret" not in (output / "run.json").read_text()
    assert "secret" not in (output / "run.json").read_text()


def test_invalid_model_output_is_not_saved(monkeypatch, tmp_path):
    secret = "provider-invalid-output-secret"
    result, output = invoke(tmp_path, monkeypatch, fake=lambda *args, **kwargs: secret)
    assert result == 2
    assert secret not in "\n".join(path.read_text() for path in output.iterdir())
    responses = json.loads((output / "responses.json").read_text())
    assert all(row["status"] == "error" and row["error_code"] == "invalid_output" for row in responses)
    run = json.loads((output / "run.json").read_text())
    assert run["segments"]["invalid_output"] == 2 and run["segments"]["provider_error"] == 0


def test_internal_parse_failure_is_safe_and_writes_no_partial_output(monkeypatch, tmp_path, capsys):
    import scripts.run_extraction_model as runner
    secret = "runner internals /private/path"

    def fail_parse(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(runner.extraction_eval, "_validate_packet", lambda packet: None)
    monkeypatch.setattr(runner.extraction_output, "parse_output", fail_parse)
    result, output = invoke(
        tmp_path, monkeypatch,
        fake=lambda *args, **kwargs: json.dumps({"candidates": [], "relations": []}),
    )

    assert result == 1
    assert not output.exists()
    captured = capsys.readouterr()
    assert "runner_error" in captured.err
    assert "invalid_output" not in captured.out + captured.err
    assert secret not in captured.out + captured.err


def test_timeout_is_distinguished(monkeypatch, tmp_path):
    from app.agent.llm import LLMError
    result, output = invoke(tmp_path, monkeypatch, fake=lambda *args, **kwargs: (_ for _ in ()).throw(LLMError("hidden", code="timeout")))
    assert result == 2
    assert all(row["error_code"] == "timeout" for row in json.loads((output / "responses.json").read_text()))
    run = json.loads((output / "run.json").read_text())
    assert run["segments"]["timeout"] == 2
    assert sum(run["provider_error_codes"].values()) == 0
    assert sum(run["provider_http_statuses"].values()) == 0


def test_existing_output_is_not_modified(monkeypatch, tmp_path):
    output = tmp_path / "run"
    output.mkdir()
    sentinel = output / "keep"
    sentinel.write_text("keep", encoding="utf-8")
    import scripts.run_extraction_model as runner
    packet_path = tmp_path / "packet.json"
    packet_path.write_text(json.dumps(packet()), encoding="utf-8")
    monkeypatch.setattr(runner.llm, "chat", lambda *args, **kwargs: json.dumps({"candidates": [], "relations": []}))
    assert runner.main(["--packet", str(packet_path), "--output", str(output)]) == 1
    assert sentinel.read_text() == "keep"


@pytest.mark.parametrize("value", [0, 3, -1])
def test_invalid_limit_is_rejected(tmp_path, monkeypatch, value):
    import scripts.run_extraction_model as runner
    packet_path = tmp_path / "packet.json"
    packet_path.write_text(json.dumps(packet()), encoding="utf-8")
    assert runner.main(["--packet", str(packet_path), "--output", str(tmp_path / "run"), "--limit", str(value)]) == 1


def test_reasoning_only_run_records_safe_metadata_and_never_accepts_reasoning(monkeypatch, tmp_path):
    from app.agent import llm
    secret = 'PRIVATE_REASONING_WITH_VALID_JSON: {"candidates":[],"relations":[]}'
    data = {'choices': [{'finish_reason': 'stop', 'message': {
        'role': 'assistant', 'reasoning_content': secret,
    }}]}
    transport = []
    monkeypatch.setattr(llm, '_payload', lambda *a, **kw: ('https://example.test', {}, {}))

    def bounded(*args, **kwargs):
        transport.append(1)
        return data

    monkeypatch.setattr(llm, '_bounded_completion', bounded)
    result, output = invoke(tmp_path, monkeypatch, fake=llm.chat, limit=1)
    assert result == 2 and len(transport) == 1
    run = json.loads((output / 'run.json').read_text())
    assert run['segments']['ok'] == 0
    assert run['segments']['provider_error'] == 1
    assert run['segments']['not_run'] == 1
    assert run['provider_error_codes']['invalid_message'] == 1
    diagnostic, = run['response_diagnostics']
    assert diagnostic['content_present'] is False
    assert diagnostic['reasoning_content_length'] == len(secret)
    assert diagnostic['finish_reason'] == 'stop'
    assert all(row['output'] == '' for row in json.loads((output / 'responses.json').read_text()))
    assert secret not in ''.join(p.read_text() for p in output.iterdir())


def test_runner_resanitizes_exception_metadata(monkeypatch, tmp_path):
    from app.agent.llm import LLMError
    error = LLMError('PRIVATE', code='invalid_message')
    error.response_metadata = {
        'content_present': False, 'reasoning_content_length': 7,
        'finish_reason': 'PRIVATE', 'content_type': ['PRIVATE'],
        'reasoning_content': 'PRIVATE', 'api_key': 'PRIVATE', 'content_length': True,
    }

    def fail(*a, **kw):
        raise error

    result, output = invoke(tmp_path, monkeypatch, fake=fail, limit=1)
    assert result == 2
    run = json.loads((output / 'run.json').read_text())
    assert run['response_diagnostics'] == [{'content_present': False, 'reasoning_content_length': 7}]
    assert 'PRIVATE' not in ''.join(p.read_text() for p in output.iterdir())


def test_run_never_accepts_fabricated_quotes_from_final_content(monkeypatch, tmp_path):
    value = {'candidates': [{
        'id': 'n1', 'name': 'concept', 'kp_type': 'concept',
        'branch_keys': ['study.knowledge_understanding'], 'gist': 'synthetic',
        'detail': '', 'chapter': '', 'quote': 'FABRICATED_PRIVATE_QUOTE',
    }], 'relations': []}
    result, output = invoke(tmp_path, monkeypatch, fake=lambda *a, **kw: json.dumps(value), limit=1)
    assert result == 2
    run = json.loads((output / 'run.json').read_text())
    assert run['segments']['invalid_output'] == 1 and run['segments']['ok'] == 0
    assert 'FABRICATED_PRIVATE_QUOTE' not in ''.join(p.read_text() for p in output.iterdir())
