from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
ANALYSIS = ROOT / "private_data" / "tencent_live_protocol_analysis_20261014-143204"
CAPTURE = ROOT / "debug" / "captures" / "20261014-143204" / "frames.jsonl"
pytestmark = pytest.mark.skipif(not ANALYSIS.is_dir(), reason="local protocol analysis unavailable")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_corrected_protocol_candidate_is_self_consistent_and_capture_bound():
    candidate = json.loads((ANALYSIS / "tencent-live-protocol-semantics-candidate-v1.json").read_text(encoding="utf-8"))
    exchange = json.loads((ANALYSIS / "exchange-analysis.json").read_text(encoding="utf-8"))
    previous = json.loads((ANALYSIS / "previous-result-analysis.json").read_text(encoding="utf-8"))
    ground = json.loads((ANALYSIS / "exchange-analysis-groundtruth.json").read_text(encoding="utf-8"))
    assert candidate["authoritative"] is False
    assert candidate["level_ceiling"] == "LEVEL_3_MULTI_SAMPLE_DISAMBIGUATED"
    assert "first_observed_game=unknown_unverified" in candidate["previous_result_mapping"]
    assert exchange["per_game_exchange_commands"]["1"] == [
        {"sequence": 23, "sub_index": 0, "command": 3011},
        {"sequence": 23, "sub_index": 1, "command": 3002},
    ]
    assert previous["association_status"]["game_1"].startswith("unknown/unverified")
    mapping = ground["resolved_mappings"]["3013.field3"]
    assert "recipient/target seat candidate" in mapping["meaning"]
    assert mapping["level"] == "LEVEL_3_MULTI_SAMPLE_DISAMBIGUATED"
    assert mapping["authority_limit"].endswith("not LEVEL_4")
    plays = json.loads((ANALYSIS / "command-3005-analysis.json").read_text(encoding="utf-8"))
    assert len(plays["records"]) == 169
    assert {record["field11_length"] for record in plays["records"]} == {20}
    unresolved = json.loads((ANALYSIS / "unresolved-fields.json").read_text(encoding="utf-8"))
    field11 = next(item for item in unresolved["items"]
                   if item.get("command") == 3005 and item.get("field") == 11)
    assert field11["observed"] == "byte-identical 20-byte blob in 169/169 records"
    assert sha(CAPTURE) == "29e91cf3ffc3169bb671b238fe7549944e60a9bc8b0a9a61d47192f6ba0e5e48"


def test_corrected_sha256sums_matches_every_listed_analysis_artifact():
    lines = (ANALYSIS / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 23
    for line in lines:
        digest, name = line.split("  ", 1)
        assert sha(ANALYSIS / name) == digest
