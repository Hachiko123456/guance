from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from guance.infrastructure.capture import canonical_capture as module
from guance.infrastructure.capture.canonical_capture import (
    CanonicalCaptureError, strict_json_loads, validate_v2_candidate,
)
from tests.local_capture.canonical_capture_fixture import (
    create_capture, decision_input, finish_capture, record_confirmed,
)

ROOT = Path(__file__).parents[2]
VALIDATOR = ROOT / "scripts" / "validate_phase7_capture_candidate.py"


def _validator():
    spec = importlib.util.spec_from_file_location("phase7_v2_validator", VALIDATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _eligible(tmp_path: Path):
    recorder, converter, source, capture_id = create_capture(tmp_path)
    record_confirmed(converter, decision_input(capture_id))
    finish_capture(recorder)
    assert converter.close().status == "eligible"
    entry = {"capture_id": capture_id,
             "relative_path": source.relative_to(tmp_path).as_posix(),
             "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    return source, capture_id, entry


def test_v2_validator_positive_path_is_streamed_and_bounded(tmp_path):
    source, capture_id, entry = _eligible(tmp_path)
    result = _validator().validate_candidate(entry, tmp_path)
    assert result["schema"] == "CanonicalLocalCaptureAcceptanceInput.v2"
    assert result["producer"] == "guance.CanonicalCaptureConverter"
    assert result["producer_version"] == "2"
    assert result["eligible"] is True
    assert result["canonical_journal_status"] == "valid"
    assert result["decision_count"] == 1
    text = json.dumps(result, sort_keys=True).lower()
    assert '"cards":' not in text and '"hand":' not in text
    assert '"observer_history":' not in text and '"legal_rows":' not in text
    assert validate_v2_candidate(source.parent, capture_id)["eligible"] is True


@pytest.mark.parametrize("target,mutation", [
    ("source", "append"),
    ("sidecar", "source_hash"),
    ("journal", "content"),
    ("manifest", "journal_hash"),
    ("manifest", "journal_rows"),
    ("manifest", "source_rows"),
    ("manifest", "bool_count"),
    ("manifest", "absolute_name"),
    ("manifest", "extra_field"),
])
def test_v2_tampering_is_bounded_and_ineligible(tmp_path, target, mutation):
    source, _capture_id, entry = _eligible(tmp_path)
    directory = source.parent
    if target == "source":
        with source.open("ab") as stream:
            stream.write(b"\n")
    elif target == "sidecar":
        path = directory / "capture_status.json"
        data = json.loads(path.read_text(encoding="utf-8")); data["source_sha256"] = "0" * 64
        path.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
    elif target == "journal":
        path = directory / "phase7_canonical_decisions.jsonl"
        data = strict_json_loads(path.read_text(encoding="utf-8").splitlines()[0])
        data["public_self_state_hash"] = "0" * 64
        path.write_text(json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    else:
        path = directory / "phase7_acceptance_input.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        if mutation == "journal_hash": data["canonical_decisions_sha256"] = "0" * 64
        elif mutation == "journal_rows": data["canonical_decisions_rows"] += 1
        elif mutation == "source_rows": data["source_rows"] += 1
        elif mutation == "bool_count": data["decision_count"] = True
        elif mutation == "absolute_name": data["source_filename"] = r"D:\\frames.jsonl"
        elif mutation == "extra_field": data["extra"] = 1
        path.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
    result = _validator().validate_candidate(entry, tmp_path)
    assert result["eligible"] is False
    assert result["missing_fact_codes"]
    assert all(code.isupper() or "_" in code for code in result["missing_fact_codes"])


def test_duplicate_manifest_member_is_rejected(tmp_path):
    source, _capture_id, entry = _eligible(tmp_path)
    path = source.parent / "phase7_acceptance_input.json"
    text = path.read_text(encoding="utf-8").rstrip()
    path.write_text(text[:-1] + ',"schema":"duplicate"}\n', encoding="utf-8")
    result = _validator().validate_candidate(entry, tmp_path)
    assert result["eligible"] is False
    assert result["missing_fact_codes"] == ["JSON_DUPLICATE_MEMBER"]


def test_blocked_status_never_becomes_positive_input(tmp_path):
    recorder, converter, source, capture_id = create_capture(tmp_path)
    converter.block("OBSERVER_IDENTITY_UNAVAILABLE")
    finish_capture(recorder)
    result = converter.close()
    assert result.status == "blocked"
    assert (source.parent / "phase7_acceptance_status.json").is_file()
    assert not (source.parent / "phase7_acceptance_input.json").exists()
    assert not (source.parent / "phase7_canonical_decisions.jsonl").exists()
    entry = {"capture_id": capture_id,
             "relative_path": source.relative_to(tmp_path).as_posix(),
             "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    checked = _validator().validate_candidate(entry, tmp_path)
    assert checked["eligible"] is False
    assert "CANONICAL_INPUT_MANIFEST_MISSING" in checked["missing_fact_codes"]


def test_validator_source_is_read_only_and_does_not_import_model_runtime():
    source = VALIDATOR.read_text(encoding="utf-8")
    assert "write_text" not in source
    assert "unlink(" not in source and "replace(" not in source
    assert "torch" not in source.lower()

@pytest.mark.parametrize("capture_id", ["CON", "name:stream", "../escape"])
def test_reserved_ads_and_traversal_capture_ids_are_rejected(tmp_path, capture_id):
    with pytest.raises(CanonicalCaptureError):
        module._safe_capture_directory(tmp_path, capture_id)


def test_symlink_capture_directory_is_rejected_when_supported(tmp_path):
    target = tmp_path / "target"; target.mkdir()
    link = tmp_path / "capture-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(CanonicalCaptureError, match="^CAPTURE_DIRECTORY_UNSAFE$"):
        module._safe_capture_directory(link, "capture-link")

def test_source_envelope_with_hidden_or_unknown_field_is_rejected(tmp_path):
    source, _capture_id, entry = _eligible(tmp_path)
    row = json.loads(source.read_text(encoding="utf-8").splitlines()[0])
    row["hidden_opponent_hands"] = [[1, 2, 3]]
    source.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
    entry["source_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    result = _validator().validate_candidate(entry, tmp_path)
    assert result["eligible"] is False
    assert result["missing_fact_codes"] == ["FRAME_METADATA_INVALID"]
