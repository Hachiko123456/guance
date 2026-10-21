from __future__ import annotations

import json
from pathlib import Path

import pytest

from guance.infrastructure.capture.offline_replay import (
    OfflineReplayError, iter_debug_frames, protocol_variant_matrix, strict_replay,
)

ROOT = Path(__file__).resolve().parents[2]
CAPTURES = ROOT / "debug" / "captures"
# Frozen materialized runtime source (hash-verified GD-RULES, Phase 2 evidence);
# the live Guandan worktree is deliberately never used as a rules/runtime root.
RULES = (ROOT / "build" / "acceptance" / "guance-model-platform-20261016"
         / "phase2" / "runtime" / "marvel-e5c82e7c-d7acbc8d" / "source")


def test_reader_never_accesses_client_payload(tmp_path: Path):
    path = tmp_path / "frames.jsonl"
    path.write_text(json.dumps({
        "schema_version": 1, "host": "hlxyxws.huanle.qq.com",
        "connection_id": "c", "sequence": 0, "received_at": 1.0,
        "direction": "client", "payload_b64": "%%%PRIVATE%%%",
        "payload_bytes": 999, "payload_sha256": "not-read",
    }) + "\n", encoding="utf-8")
    values = list(iter_debug_frames(path))
    assert len(values) == 1 and values[0].frame.payload == b""


def test_reader_rejects_direction_unavailable_without_decoding_payload():
    path = CAPTURES / "20260929-141145" / "frames.jsonl"
    with pytest.raises(OfflineReplayError, match="DIRECTION_UNAVAILABLE"):
        next(iter_debug_frames(path))


def test_latest_protocol_matrix_contains_repaired_exact_shapes_and_no_rejection():
    path = CAPTURES / "20261014-231315" / "frames.jsonl"
    value = protocol_variant_matrix("20261014-231315", path)
    assert value["first_parser_rejection"] is None
    shapes = {(row["command_id"], row["group_shape"]): row for row in value["variants"]}
    assert (3005, "1:0,2:0,4:0,6:0,7:2,8:0,9:0,11:2") in shapes
    assert (3005, "2:0,4:0,5:0,6:0,7:2,8:0,9:0,11:2") in shapes
    assert (3006, "2:0,5:0") in shapes
    assert shapes[(3003, "1:0,3:0,4:2,5:2")]["parser_expected_result"] == "informational"
    assert shapes[(3007, "1:0,2:2")]["parser_expected_result"] == "informational"
    assert value["client_payload_decoded"] is False


def test_latest_capture_replays_two_epochs_without_protocol_or_transport_errors():
    value = strict_replay(
        "20261014-231315", CAPTURES / "20261014-231315" / "frames.jsonl",
        rules_source_root=RULES,
    )
    assert value["protocol_rejected"] == 0
    assert value["unexpected_unknown_command"] == 0
    assert value["frame_integrity_errors"] == 0
    assert value["sequence_errors"] == 0
    assert value["engine_paused"] is False
    assert [(row["deal_count"], row["start_count"], row["settlement_count"])
            for row in value["connection_epochs"]] == [(1, 1, 1), (1, 1, 1)]
    assert all(row["unsupported_code"] == "PREVIOUS_RESULT_UNAVAILABLE"
               for row in value["connection_epochs"])
    assert value["supported_decision_count"] == 0


@pytest.mark.skipif(not RULES.is_dir(), reason="verified Guandan source unavailable")
def test_normal_exchange_capture_produces_bounded_supported_and_unsupported_decisions():
    value = strict_replay(
        "20261014-143204", CAPTURES / "20261014-143204" / "frames.jsonl",
        rules_source_root=RULES,
    )
    assert value["protocol_rejected"] == 0
    assert value["frame_integrity_errors"] == 0
    assert value["sequence_errors"] == 0
    assert value["engine_paused"] is False
    assert value["event_counts"]["DealEvent"] == 5
    assert value["event_counts"]["SettlementEvent"] == 5
    assert value["supported_decision_count"] > 0
    assert value["supported_decision_count"] == value["model_adapter_eligible_count"]
    assert set(value["error_counts"]) <= {
        "LIVE_DECLARATION_AMBIGUOUS", "PREVIOUS_RESULT_ASSOCIATION_UNSUPPORTED",
    }
    assert all(item["legal_row_count"] > 0 for item in value["supported_decisions"])
