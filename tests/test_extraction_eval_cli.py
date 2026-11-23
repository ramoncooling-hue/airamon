"""Exercise the real CLI in subprocesses using synthetic files only."""

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest

from test_extraction_eval import TEXT, packet, responses, reviewed


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "evaluate_extraction.py"


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def invoke(tmp_path, *, data=None, result=None, review=None, output=None, extra=()):
    packet_file = write_json(tmp_path / "packet.json", packet() if data is None else data)
    response_file = write_json(tmp_path / "responses.json", responses() if result is None else result)
    arguments = [
        "--packet",
        str(packet_file),
        "--responses",
        str(response_file),
        "--output",
        str(tmp_path / "report" if output is None else output),
    ]
    if review is not None:
        arguments += ["--review", str(write_json(tmp_path / "review.json", review))]
    arguments += list(extra)
    completed = subprocess.run(
        [sys.executable, "-B", str(SCRIPT), *arguments], cwd=tmp_path, capture_output=True, text=True, timeout=15
    )
    return completed


def write_source(path, content="source bytes"):
    path.write_text(content, encoding="utf-8")
    return path


def test_pending_cli_produces_three_files_and_readable_candidates(tmp_path):
    completed = invoke(tmp_path)
    assert completed.returncode == 2, completed.stderr
    output = tmp_path / "report"
    assert {path.name for path in output.iterdir()} == {"report.json", "report.md", "review-template.json"}
    report = json.loads((output / "report.json").read_text())
    assert report["status"] == "incomplete" and report["semantic_scores"]["nodes"]["precision"] is None
    markdown = (output / "report.md").read_text()
    assert "Alpha is a concept." in markdown and "supports" in markdown and "ga" in markdown
    for path in output.iterdir():
        assert TEXT not in path.read_text() and "PRIVATE_SOURCE_TAIL" not in path.read_text()
    assert "Traceback" not in completed.stderr


def test_empty_real_responses_create_pending_missing_coverage_report(tmp_path):
    data = packet(
        dataset_kind="real_textbook", annotation_status="draft", annotator="", gold_nodes=[], gold_relations=[]
    )
    completed = invoke(tmp_path, data=data, result=[])
    assert completed.returncode == 2, completed.stderr
    report = json.loads((tmp_path / "report/report.json").read_text())
    assert report["segments"]["missing"] == 1 and report["segments"]["attempted"] == 0
    assert report["structural_validity"]["rate"] is None
    assert report["semantic_scores"]["nodes"]["recall"] is None
    assert "missing" in (tmp_path / "report/report.md").read_text()


def test_cli_keeps_not_run_out_of_attempted_and_reports_it_separately(tmp_path):
    data = packet(gold_nodes=[], gold_relations=[])
    base = data["segments"][0]
    data["segments"] = [dict(base, id="s1"), dict(base, id="s2")]
    result = [{"segment_id": "s1", "status": "error", "output": "", "error_code": "not_run"}]

    completed = invoke(tmp_path, data=data, result=result)

    assert completed.returncode == 2, completed.stderr
    report = json.loads((tmp_path / "report/report.json").read_text())
    assert report["segments"] == {
        "total": 2,
        "attempted": 0,
        "valid": 0,
        "missing": 1,
        "not_run": 1,
        "error": 0,
        "provider_error": 0,
        "timeout": 0,
        "invalid_output": 0,
    }
    markdown = (tmp_path / "report/report.md").read_text()
    assert "| total | attempted | valid | missing | not run |" in markdown
    assert "| 2 | 0 | 0 | 1 | 1 | 0 | 0 | 0 | 0 |" in markdown


def test_complete_synthetic_review_returns_zero_and_keeps_template_pending(tmp_path):
    completed = invoke(tmp_path, review=reviewed())
    assert completed.returncode == 0, completed.stderr
    report = json.loads((tmp_path / "report/report.json").read_text())
    assert report["status"] == "complete" and report["score_scope"] == "synthetic"
    assert report["semantic_scores"]["nodes"]["recall"] == 1.0
    template = json.loads((tmp_path / "report/review-template.json").read_text())
    assert template["method"] == "pending"
    assert all(row["supported"] is None for row in template["nodes"])
    assert "Synthetic scores" in (tmp_path / "report/report.md").read_text()


def test_validated_review_is_separate_and_template_remains_blank(tmp_path):
    completed = invoke(tmp_path, review=reviewed())
    assert completed.returncode == 0, completed.stderr
    output = tmp_path / "report"
    assert {path.name for path in output.iterdir()} == {
        "report.json",
        "report.md",
        "review-template.json",
        "review.json",
    }
    report = json.loads((output / "report.json").read_text())
    review_file = json.loads((output / "review.json").read_text())
    template = json.loads((output / "review-template.json").read_text())
    expected = hashlib.sha256(
        json.dumps(review_file, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert report["review_sha256"] == expected
    assert report["judgments"] == {"nodes": review_file["nodes"], "relations": review_file["relations"]}
    assert review_file["packet_sha256"] == report["packet_sha256"]
    assert review_file["responses_sha256"] == report["responses_sha256"]
    assert template["method"] == "pending" and template["reviewer"] == ""
    assert all(row["supported"] is None for row in template["nodes"])


def test_repeated_source_hashes_enable_real_completion_without_paths(tmp_path):
    source = write_source(tmp_path / "source.pdf", "synthetic source bytes")
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    data = packet(dataset_kind="real_textbook")
    data["segments"][0]["source_sha256"] = source_hash
    completed = invoke(
        tmp_path,
        data=data,
        review=reviewed(data, method="human"),
        extra=("--source", str(source), "--source", str(source)),
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads((tmp_path / "report/report.json").read_text())
    assert report["source_verification"]["status"] == "verified"
    assert str(source) not in json.dumps(report)
    assert "synthetic source bytes" not in json.dumps(report)
    markdown = (tmp_path / "report/report.md").read_text()
    assert str(source) not in markdown and "synthetic source bytes" not in markdown


def test_source_hashing_deduplicates_paths_and_bounds_count_and_aggregate(tmp_path, monkeypatch):
    from scripts import evaluate_extraction

    source = write_source(tmp_path / "source.pdf", "synthetic source bytes")
    calls = []
    original = evaluate_extraction._hash_source

    def counted(path, **kwargs):
        calls.append(path)
        return original(path, **kwargs)

    monkeypatch.setattr(evaluate_extraction, "_hash_source", counted)
    result = evaluate_extraction._hash_sources([str(source), str(source)])
    assert len(result) == 1 and len(calls) == 1

    monkeypatch.setattr(evaluate_extraction, "_hash_source", lambda *args, **kwargs: pytest.fail("source was read"))
    too_many = [str(source)] * (evaluate_extraction.MAX_SOURCE_COUNT + 1)
    with pytest.raises(ValueError, match="Too many source"):
        evaluate_extraction._hash_sources(too_many)

    large_sources = []
    for index in range(3):
        path = tmp_path / f"sparse-{index}.pdf"
        with path.open("wb") as stream:
            stream.truncate(evaluate_extraction.MAX_SOURCE_BYTES)
        large_sources.append(str(path))
    with pytest.raises(ValueError, match="Aggregate source size"):
        evaluate_extraction._hash_sources(large_sources)


def test_real_completion_without_source_flag_stays_pending(tmp_path):
    data = packet(dataset_kind="real_textbook")
    completed = invoke(tmp_path, data=data, review=reviewed(data, method="human"))
    assert completed.returncode == 2
    report = json.loads((tmp_path / "report/report.json").read_text())
    assert "source_unverified" in report["incomplete_reasons"]


def test_source_mismatch_is_sanitized_before_output_creation(tmp_path):
    source = write_source(tmp_path / "source.pdf", "wrong source")
    data = packet(dataset_kind="real_textbook")
    completed = invoke(
        tmp_path,
        data=data,
        review=reviewed(data, method="human"),
        extra=(
            "--source",
            str(source),
        ),
    )
    assert completed.returncode == 1 and not (tmp_path / "report").exists()
    assert str(source) not in completed.stdout + completed.stderr


def test_source_must_be_a_bounded_regular_file(tmp_path):
    source = tmp_path / "oversized-source.pdf"
    with source.open("wb") as stream:
        stream.truncate(1024 * 1024 * 1024 + 1)
    data = packet(dataset_kind="real_textbook")
    completed = invoke(
        tmp_path,
        data=data,
        extra=(
            "--source",
            str(source),
        ),
    )
    assert completed.returncode == 1 and not (tmp_path / "report").exists()
    assert str(source) not in completed.stdout + completed.stderr


def test_source_fifo_is_rejected_without_blocking(tmp_path):
    source = tmp_path / "source-fifo"
    os.mkfifo(source)
    data = packet(dataset_kind="real_textbook")
    completed = invoke(
        tmp_path,
        data=data,
        extra=(
            "--source",
            str(source),
        ),
    )
    assert completed.returncode == 1 and not (tmp_path / "report").exists()
    assert str(source) not in completed.stdout + completed.stderr


def test_existing_output_is_not_modified(tmp_path):
    output = tmp_path / "report"
    output.mkdir()
    sentinel = output / "report.json"
    sentinel.write_text("existing report")
    completed = invoke(tmp_path)
    assert completed.returncode == 1
    assert sentinel.read_text() == "existing report"
    assert list(output.iterdir()) == [sentinel]


@pytest.mark.parametrize("alias", ["packet", "responses", "symlink", "broken_symlink"])
def test_output_aliases_and_symlinks_are_rejected(tmp_path, alias):
    if alias in ("packet", "responses"):
        output = tmp_path / (alias + ".json")
    else:
        output = tmp_path / "alias"
        if alias == "symlink":
            (tmp_path / "real").mkdir()
        output.symlink_to(tmp_path / "real", target_is_directory=True)
    completed = invoke(tmp_path, output=output)
    assert completed.returncode == 1
    assert not (tmp_path / "real/report.json").exists()
    assert not (tmp_path / "real/new").exists()


def test_symlinked_output_parent_is_resolved_to_a_safe_new_directory(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    completed = invoke(tmp_path, output=tmp_path / "link/new")
    assert completed.returncode == 2, completed.stderr
    assert (tmp_path / "real/new/report.json").is_file()
    assert not (tmp_path / "link/new").is_symlink()


@pytest.mark.parametrize(
    "raw",
    [
        '{"private":"sk-SECRET",',
        '{"segments":[],"segments":[]}',
        '{"segments":[],"segm\\u0065nts":[]}',
        '{"schema_version":NaN}',
        '{"schema_version":Infinity}',
        '{"schema_version":1e999}',
        "[" * 1000 + "]" * 1000,
        '{"schema_version":' + "9" * 5000 + "}",
        b"\xff\xfeprivate",
    ],
)
def test_bad_json_is_sanitized_and_creates_no_report(tmp_path, raw):
    private = tmp_path / "PRIVATE_LOCAL_SOURCE_sk-SECRET.json"
    private.write_bytes(raw if isinstance(raw, bytes) else raw.encode())
    response_file = write_json(tmp_path / "responses.json", [])
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            str(SCRIPT),
            "--packet",
            str(private),
            "--responses",
            str(response_file),
            "--output",
            str(tmp_path / "report"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 1
    assert not (tmp_path / "report").exists()
    assert completed.stderr and "Traceback" not in completed.stderr
    assert all(
        secret not in completed.stdout + completed.stderr
        for secret in (str(tmp_path), "PRIVATE_LOCAL_SOURCE", "sk-SECRET", raw if type(raw) is str else "private")
    )


def test_malformed_review_and_stale_bindings_create_no_output(tmp_path):
    review = reviewed()
    review["responses_sha256"] = "0" * 64
    completed = invoke(tmp_path, review=review)
    assert completed.returncode == 1 and not (tmp_path / "report").exists()


def test_invalid_provider_output_never_reaches_files_or_stderr(tmp_path):
    result = responses()
    result[0]["output"] = "sk-PRIVATE_PROVIDER /private/source.pdf"
    completed = invoke(tmp_path, result=result)
    assert completed.returncode == 2, completed.stderr
    for path in (tmp_path / "report").iterdir():
        assert "sk-PRIVATE_PROVIDER" not in path.read_text() and "/private/source.pdf" not in path.read_text()
    assert "sk-PRIVATE_PROVIDER" not in completed.stdout + completed.stderr


def test_argument_errors_do_not_echo_private_arguments(tmp_path):
    completed = subprocess.run(
        [sys.executable, "-B", str(SCRIPT), "--unknown", "sk-SECRET/private/path"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 1
    assert "sk-SECRET" not in completed.stderr and "/private/path" not in completed.stderr


def test_oversize_json_is_rejected_before_decoding(tmp_path):
    private = tmp_path / "oversized.json"
    with private.open("wb") as stream:
        stream.truncate(64 * 1024 * 1024 + 1)
    response_file = write_json(tmp_path / "responses.json", [])
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            str(SCRIPT),
            "--packet",
            str(private),
            "--responses",
            str(response_file),
            "--output",
            str(tmp_path / "report"),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 1 and not (tmp_path / "report").exists()
    assert str(private) not in completed.stderr


def test_fifo_input_fails_without_blocking(tmp_path):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    response_file = write_json(tmp_path / "responses.json", [])
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            str(SCRIPT),
            "--packet",
            str(fifo),
            "--responses",
            str(response_file),
            "--output",
            str(tmp_path / "report"),
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert completed.returncode == 1 and not (tmp_path / "report").exists()


def test_imports_are_pure_and_do_not_load_runtime_database_or_provider(tmp_path):
    code = """
import importlib.abc, importlib.util, sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root))
blocked = ('app.runtime', 'app.main', 'app.settings', 'sqlite3', 'httpx', 'requests', 'fastapi', 'app.agent.llm')
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + '.') for name in blocked):
            raise AssertionError('Forbidden runtime import')
sys.meta_path.insert(0, Guard())
def audit(event, args):
    if event in ('sqlite3.connect', 'socket.connect', 'socket.bind', 'os.mkdir', 'subprocess.Popen'):
        raise AssertionError('Forbidden side effect')
sys.addaudithook(audit)
from app.memory import extraction_eval
spec = importlib.util.spec_from_file_location('offline_cli', root / 'scripts/evaluate_extraction.py')
spec.loader.exec_module(importlib.util.module_from_spec(spec))
assert not any(name in sys.modules for name in blocked)
print('pure imports')
"""
    completed = subprocess.run(
        [sys.executable, "-B", "-c", code, str(ROOT)], cwd=tmp_path, capture_output=True, text=True, timeout=15
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "pure imports" and list(tmp_path.iterdir()) == []
