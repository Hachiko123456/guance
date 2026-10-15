from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from guance.infrastructure.model.marvel_v2 import CHANNEL_SLICES, CardMapping
from guance.infrastructure.model.marvel_v2.contracts import MarvelProtocolError

ROOT = Path(__file__).resolve().parents[2]
ORIGINAL = Path(__file__).parent / "fixtures" / "marvel_v2"
DECLARATION = ROOT / "tests" / "domain" / "fixtures" / "guandan_declaration_v1"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_fixed_handoff_hashes_and_contract_shapes():
    provenance = json.loads((ORIGINAL / "provenance.json").read_text(encoding="utf-8"))
    expected = provenance["files"]
    assert {name: {"sha256": _sha(ORIGINAL / name), "bytes": (ORIGINAL / name).stat().st_size}
            for name in expected} == expected
    channels = json.loads((ORIGINAL / "guandan_marvel_channels_v2.json").read_text())
    rows = json.loads((ORIGINAL / "guandan_marvel_action_row_v2.json").read_text())
    assert len(channels["channels"]) == 512
    assert tuple(channels["slices"]) == CHANNEL_SLICES
    assert [item["index"] for item in channels["channels"]] == list(range(512))
    assert rows["row_shape"] == [8]
    assert rows["max_rows"] == 256
    assert len(rows["columns"]) == 8


def test_declaration_handoff_hashes_and_coverage_are_fixed():
    assert _sha(DECLARATION / "guandan_declaration_rules_v1.json") == "29772a3ab8a61d9f603e8d57f2432abdec6f5e591927b94a7beb87fc74a77287"
    assert _sha(DECLARATION / "guandan_declaration_golden_vectors_v1.json") == "e183a6e4a9cbf01dffe424d4054ebab433f6107704680eab6e783607c812c7ae"
    vectors = json.loads((DECLARATION / "guandan_declaration_golden_vectors_v1.json").read_text())
    assert vectors["vector_count"] == {
        "base_negative": 48, "base_positive": 32, "negative": 50,
        "positive": 73, "special_boundary": 43, "total": 123,
    }
    assert vectors["level_parity"]["matrix_case_count"] == 338


def test_108_card_mapping_and_13_levels_match_handoff():
    mapping = json.loads((ORIGINAL / "guance_guandan_card_mapping.json").read_text())
    CardMapping.validate()
    assert len(mapping["cards"]) == 108
    assert [CardMapping.to_wire(card_id) for card_id in range(108)] == [
        item["guance_physical_code"] for item in mapping["cards"]
    ]
    assert [CardMapping.from_wire(item["guance_physical_code"]) for item in mapping["cards"]] == list(range(108))
    assert {str(level): CardMapping.level_to_rank(level) for level in range(1, 14)} == mapping["level_rank_mapping"]["guance_to_guandan"]


@pytest.mark.parametrize("bad", [-1, 108, True, "0", None])
def test_card_mapping_rejects_unknown_and_out_of_range(bad):
    with pytest.raises((MarvelProtocolError, ValueError, TypeError)):
        CardMapping.to_wire(bad)
