import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "prepare_extraction_annotation.py"


def packet(**changes):
    text = "<script>alert('x')</script> 这是冻结证据。"
    value = {
        "schema_version": 1,
        "dataset_id": "synthetic-annotation",
        "dataset_kind": "synthetic",
        "split": "development",
        "used_for_tuning": False,
        "annotation_status": "draft",
        "annotator": "",
        "mapping": {
            "branch_keys": ["study.knowledge_understanding"],
            "node_types": ["concept"],
            "relation_kinds": ["supports"],
            "field_mappings": {},
        },
        "segments": [{
            "id": "s1", "chapter": "第1章", "source_sha256": "a" * 64, "page": 1,
            "text": text,
            "text_sha256": __import__("hashlib").sha256(text.encode()).hexdigest(),
        }],
        "gold_nodes": [],
        "gold_relations": [],
    }
    value.update(changes)
    return value


def run(tmp_path, data=None, output=None, extra=()):
    packet_path = tmp_path / "packet.json"
    packet_path.write_text(json.dumps(data or packet(), ensure_ascii=False), encoding="utf-8")
    output = output or tmp_path / "worksheet"
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPT), "--packet", str(packet_path), "--output", str(output), *extra],
        cwd=ROOT, capture_output=True, text=True, timeout=15,
    )


def test_prepares_bound_template_and_escaped_html(tmp_path):
    result = run(tmp_path)
    assert result.returncode == 0, result.stderr
    output = tmp_path / "worksheet"
    draft = json.loads((output / "annotation-template.json").read_text())
    assert draft["annotation_status"] == "draft"
    assert draft["gold_nodes"] == [] and draft["gold_relations"] == []
    assert draft["segment_reviews"] == [{"segment_id": "s1", "status": "pending"}]
    html = (output / "annotation.html").read_text()
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" in html
    assert "Packet SHA256:" in (output / "README.md").read_text()
    from app.memory import extraction_eval
    assert extraction_eval._canonical_sha(draft) != ""


def test_existing_review_status_is_reset_when_gold_is_cleared(tmp_path):
    data = packet(segment_reviews=[{"segment_id": "s1", "status": "reviewed_no_gold"}])
    result = run(tmp_path, data=data)
    assert result.returncode == 0, result.stderr
    draft = json.loads((tmp_path / "worksheet/annotation-template.json").read_text())
    assert draft["segment_reviews"] == [{"segment_id": "s1", "status": "pending"}]


def test_rejects_existing_output_without_modifying_it(tmp_path):
    output = tmp_path / "worksheet"
    output.mkdir()
    sentinel = output / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    result = run(tmp_path, output=output)
    assert result.returncode == 1
    assert sentinel.read_text() == "keep"
    assert not (output / "annotation.html").exists()


@pytest.mark.parametrize("bad", [
    {"schema_version": 1},
    {"schema_version": 1, "segments": []},
])
def test_rejects_invalid_packet_without_echoing_input(tmp_path, bad):
    result = run(tmp_path, data=bad)
    assert result.returncode == 1
    assert str(tmp_path) not in result.stdout + result.stderr


def test_rejects_output_inside_packet_file_path(tmp_path):
    packet_path = tmp_path / "packet.json"
    packet_path.write_text(json.dumps(packet()), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-B", str(SCRIPT), "--packet", str(packet_path), "--output", str(packet_path / "child")],
        cwd=ROOT, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 1
