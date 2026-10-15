"""Synthetic-only tests for bounded Phase 7 protocol rejection diagnostics."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from guance.infrastructure.protocol.live import GAME_HOST
from tests.training_data.test_legacy import _field, _packet

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "diagnose_local_capture_protocol.py"


def _module():
    spec = importlib.util.spec_from_file_location("phase7_protocol_diagnostic", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(payload: bytes, sequence: int, direction: str) -> dict[str, object]:
    return {
        "schema_version": 1, "host": GAME_HOST, "received_at": float(sequence),
        "connection_id": "synthetic-connection", "sequence": sequence,
        "direction": direction, "payload_bytes": len(payload),
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "payload_b64": base64.b64encode(payload).decode("ascii"),
    }


def _capture(tmp_path: Path, rows: list[dict[str, object]], capture_id: str = "synthetic-capture"):
    directory = tmp_path / "debug" / "captures" / capture_id
    directory.mkdir(parents=True)
    path = directory / "frames.jsonl"
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return path, {"capture_id": capture_id,
                  "relative_path": path.relative_to(tmp_path).as_posix(),
                  "source_sha256": digest}


def test_diagnostic_maps_missing_field_deterministically_without_decoding_client_payload(tmp_path):
    module = _module()
    client = _row(b"must-not-be-decoded", 1, "client")
    client["payload_b64"] = "%%%invalid-client-base64%%%"
    client["payload_sha256"] = "not-read"
    # 3005 has actor/combo/remaining/count/cards but deliberately lacks next actor field 3.
    body = (_field(2, 0) + _field(4, 1) + _field(5, 26)
            + _field(6, 1) + _field(7, bytes([17])))
    server = _row(_packet(3005, 6, body), 2, "server")
    path, entry = _capture(tmp_path, [client, server])
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    first = module.diagnose_capture(entry, tmp_path)
    second = module.diagnose_capture(entry, tmp_path)
    assert first == second
    assert first["frame_ordinal"] == 2
    assert first["source_sequence"] == 2
    assert first["command_id"] == 3005
    assert first["parser_stage"] == "command_contract"
    assert first["bounded_error_code"] == "COMMAND_REQUIRED_FIELD_MISSING"
    assert first["required_field_ids"] == [3]
    assert first["missing_fact_codes"] == ["NEXT_ACTOR_UNAVAILABLE"]
    assert first["closed_sidecar_status"] == "CAPTURE_CLOSED_SIDECAR_MISSING"
    assert first["local_repair_allowed"] is False
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    text = json.dumps(first, sort_keys=True)
    assert "must-not-be-decoded" not in text and "invalid-client-base64" not in text


def test_diagnostic_detects_sequence_gap_before_server_payload_parse(tmp_path):
    module = _module()
    first = _row(b"unused", 1, "client")
    first["payload_b64"] = "%%%not-read%%%"
    second = _row(b"not-an-aggregate", 3, "server")
    _path, entry = _capture(tmp_path, [first, second])
    result = module.diagnose_capture(entry, tmp_path)
    assert result["bounded_error_code"] == "FRAME_SEQUENCE_GAP"
    assert result["parser_stage"] == "transport_sequence"
    assert result["frame_ordinal"] == 2
    assert result["source_sequence"] == 3


def test_command_classifier_rejects_type_range_and_unknown_without_values():
    module = _module()
    from guance.infrastructure.protocol import wire

    code, fields, facts = module._command_problem(3006, {2: [(0, 0)], 3: [(0, 1)]}, wire)
    assert (code, fields, facts) == (
        "COMMAND_REQUIRED_FIELD_MISSING", (4,),
        ("NEXT_ACTOR_UNAVAILABLE", "PASS_RESET_FACT_UNAVAILABLE"),
    )
    code, fields, _facts = module._command_problem(
        3006, {2: [(2, b"x")], 3: [(0, 1)], 4: [(0, 0)]}, wire)
    assert (code, fields) == ("COMMAND_REQUIRED_FIELD_TYPE_INVALID", (2,))
    code, fields, _facts = module._command_problem(
        3006, {2: [(0, 7)], 3: [(0, 1)], 4: [(0, 0)]}, wire)
    assert (code, fields) == ("COMMAND_VALUE_OUT_OF_RANGE", (2, 3, 4))
    assert module._command_problem(9999, {}, wire) == ("COMMAND_UNKNOWN", (), ())


def test_output_guard_rejects_content_paths_and_untrusted_text():
    module = _module()
    safe = {"capture_id": "synthetic", "source_sha256": "0" * 64,
            "command_id": 3005, "required_field_ids": [3],
            "bounded_error_code": "COMMAND_REQUIRED_FIELD_MISSING"}
    module._validate_output(safe)
    for value in (
        {"payload": "secret"}, {"cards": [1]}, {"capture_id": r"D:\private"},
        {"bounded_error_code": "bad\nmessage"},
    ):
        with pytest.raises(ValueError, match="^unsafe_output$"):
            module._validate_output(value)


def test_internal_failure_is_bounded():
    module = _module()
    value = module._failure(frame_ordinal=1, source_sequence=1, connection_ordinal=1,
                            direction="server", aggregate_message_ordinal=1,
                            command_id=3005, parser_stage="unknown-stage",
                            bounded_error_code="raw exception text")
    assert value["bounded_error_code"] == "INTERNAL_DIAGNOSTIC_REJECTED"
    assert value["parser_stage"] == "command_contract"
    assert "raw exception text" not in json.dumps(value)
