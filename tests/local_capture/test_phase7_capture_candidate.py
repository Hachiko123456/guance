"""Synthetic future-capture contract tests; never read real capture contents."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "validate_phase7_capture_candidate.py"


def _module():
    spec = importlib.util.spec_from_file_location("phase7_candidate_validator", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _candidate(tmp_path: Path):
    capture_id = "future-capture"
    directory = tmp_path / "debug" / "captures" / capture_id
    directory.mkdir(parents=True)
    frames = directory / "frames.jsonl"
    rows = [
        {"schema_version": 1, "host": "hlxyxws.huanle.qq.com", "received_at": 1.0,
         "connection_id": "connection", "sequence": 1, "direction": "client",
         "payload_bytes": 999, "payload_sha256": "not-read", "payload_b64": "%%%not-read%%%"},
        {"schema_version": 1, "host": "hlxyxws.huanle.qq.com", "received_at": 2.0,
         "connection_id": "connection", "sequence": 2, "direction": "server",
         "payload_bytes": 0, "payload_sha256": hashlib.sha256(b"").hexdigest(), "payload_b64": ""},
    ]
    frames.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    raw = frames.read_bytes(); source_hash = hashlib.sha256(raw).hexdigest()
    sidecar = {
        "schema": "guance-capture-closed-sidecar-v1", "schema_version": 1,
        "capture_schema": "guance-debug-frame-jsonl-v1",
        "producer": "guance.DebugFrameRecorder", "producer_version": "1",
        "protocol_identity": "TencentDaguandanObservedProtocol.v20261014",
        "capture_id": capture_id, "source_filename": "frames.jsonl",
        "source_sha256": source_hash, "source_bytes": len(raw), "state": "closed",
        "started_at": capture_id, "closed_at": "2026-10-14T00:00:00+08:00",
        "close_reason": "recorder_closed", "frames": 2, "bytes": len(raw),
        "directions": {"client": 1, "server": 1},
        "hosts": {"hlxyxws.huanle.qq.com": 2},
        "connection_epochs": [{"ordinal": 1, "start_sequence": 1, "end_sequence": 2,
                               "frames": 2, "directions": {"client": 1, "server": 1}}],
        "final_sequence": 2, "current_game_id": "",
        "games": [{"game_id": "game-0001", "status": "settled", "start_frame_index": 0,
                   "end_frame_index": 1, "start_sequence": 1, "end_sequence": 2}],
    }
    sidecar_path = directory / "capture_status.json"; _write_json(sidecar_path, sidecar)
    sidecar_hash = hashlib.sha256(sidecar_path.read_bytes()).hexdigest()
    acceptance = {
        "schema": "CanonicalLocalCaptureAcceptanceInput.v1", "schema_version": 1,
        "producer": "guance.CanonicalCaptureConverter", "producer_version": "1",
        "capture_id": capture_id, "capture_schema": "guance-debug-frame-jsonl-v1",
        "protocol_identity": "TencentDaguandanObservedProtocol.v20261014",
        "target_host_identity": "hlxyxws.huanle.qq.com",
        "source_filename": "frames.jsonl", "source_sha256": source_hash,
        "source_bytes": len(raw), "source_rows": 2,
        "direction_counts": {"client": 1, "server": 1},
        "connection_epochs": sidecar["connection_epochs"], "game_count": 1,
        "decision_count": 1, "level": 8, "observer_seat": 0,
        "observer_evidence": "authoritative.semantic.event",
        "counter_contract": {"frame": "source_sequence", "state": "canonical_state_version",
                             "action": "public_action_index", "token": "marvel_token_step",
                             "commit": "marvel_committed_step"},
        "capabilities": {name: True for name in sorted({
            "public_self_canonical", "exact_four_observer_history", "private_prefix",
            "declaration", "tribute_return", "token_step", "committed_step",
            "legal_action_counters"})},
        "quality": {name: True for name in sorted({
            "no_loss", "no_gap", "no_partial", "no_quarantine"})},
        "closed_sidecar_sha256": sidecar_hash,
    }
    _write_json(directory / "phase7_acceptance_input.json", acceptance)
    entry = {"capture_id": capture_id, "relative_path": frames.relative_to(tmp_path).as_posix(),
             "source_sha256": source_hash}
    return frames, entry, sidecar_path, directory / "phase7_acceptance_input.json"


def test_v1_candidate_is_compatibility_only_and_client_payload_is_not_decoded(tmp_path):
    module = _module(); frames, entry, _sidecar, _acceptance = _candidate(tmp_path)
    before = hashlib.sha256(frames.read_bytes()).hexdigest()
    result = module.validate_candidate(entry, tmp_path)
    assert result["eligible"] is False
    assert result["missing_fact_codes"] == ["V1_NOT_POSITIVE_ELIGIBLE"]
    assert result["sidecar_status"] == "CAPTURE_CLOSED_SIDECAR_VALID"
    assert result["decision_count"] == 1
    assert hashlib.sha256(frames.read_bytes()).hexdigest() == before
    assert "not-read" not in json.dumps(result, sort_keys=True)


@pytest.mark.parametrize("mutation", [
    "missing", "malformed", "wrong_hash", "wrong_bytes", "wrong_rows",
    "wrong_directions", "non_closed", "incomplete_game", "wrong_final_sequence",
])
def test_sidecar_failures_block_candidate(tmp_path, mutation):
    module = _module(); _frames, entry, sidecar_path, _acceptance = _candidate(tmp_path)
    if mutation == "missing":
        sidecar_path.unlink()
    elif mutation == "malformed":
        sidecar_path.write_text("{", encoding="utf-8")
    else:
        data = json.loads(sidecar_path.read_text(encoding="utf-8"))
        if mutation == "wrong_hash": data["source_sha256"] = "0" * 64
        elif mutation == "wrong_bytes": data["source_bytes"] += 1
        elif mutation == "wrong_rows": data["frames"] += 1
        elif mutation == "wrong_directions": data["directions"] = {"server": 2}
        elif mutation == "non_closed": data["state"] = "capturing"
        elif mutation == "incomplete_game": data["games"][0]["status"] = "incomplete"
        elif mutation == "wrong_final_sequence": data["final_sequence"] = 99
        _write_json(sidecar_path, data)
    result = module.validate_candidate(entry, tmp_path)
    assert result["eligible"] is False
    expected = "CAPTURE_CLOSED_SIDECAR_MISSING" if mutation == "missing" else "CAPTURE_CLOSED_SIDECAR_INVALID"
    assert expected in result["missing_fact_codes"]


def test_acceptance_input_mismatch_and_capability_gaps_block(tmp_path):
    module = _module(); _frames, entry, _sidecar, acceptance_path = _candidate(tmp_path)
    data = json.loads(acceptance_path.read_text(encoding="utf-8"))
    data["source_rows"] = 3
    data["capabilities"]["token_step"] = False
    data["quality"]["no_gap"] = False
    _write_json(acceptance_path, data)
    result = module.validate_candidate(entry, tmp_path)
    assert result["eligible"] is False
    assert {"SOURCE_IDENTITY_MISMATCH", "CAPABILITIES_INCOMPLETE", "QUALITY_INCOMPLETE"} <= set(
        result["missing_fact_codes"])


def test_validator_never_creates_or_repairs_sidecar():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "capture_status.json\").write" not in source
    assert "write_text" not in source
    assert "unlink(" not in source
    assert "replace(" not in source
