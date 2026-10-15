from __future__ import annotations

import re

from guance.public_state_contract import (
    guandan_card_id_to_wire, guandan_rank_to_wire_rank,
    guandan_suit_to_wire_suit, wire_card_to_guandan_id,
    wire_rank_to_guandan_rank, wire_suit_to_guandan_suit,
)
from declaration_fixture_oracle import (
    PROVENANCE, PROVENANCE_PATH, RULES, RULES_PATH, SHA_PATH, VECTORS,
    VECTORS_PATH, semantic_sha256, sha256,
)

RULES_SHA = "29772a3ab8a61d9f603e8d57f2432abdec6f5e591927b94a7beb87fc74a77287"
VECTORS_SHA = "e183a6e4a9cbf01dffe424d4054ebab433f6107704680eab6e783607c812c7ae"
RULES_SEMANTIC = "96b1fdccf795af4e51361e8bd155d5e46c663d50ea1d1dda22a3a6021ce663dc"
VECTORS_SEMANTIC = "8573a4def03f9bd72d0bfddbca136ec3bec5ff0255fb7b2b578cffb012f6c739"
AUTHORITY_COMMIT = "8ea264c1bde4b8faac24560afa27d37955232028"
PROTOCOL = "GD-ACTION-0.2-MARVEL|GD-ENV-0.2-MARVEL|GD-ENCODING-0.2-MARVEL"


def test_declaration_fixture_hash_schema_protocol_and_old_paths():
    assert sha256(RULES_PATH) == RULES_SHA
    assert sha256(VECTORS_PATH) == VECTORS_SHA
    assert RULES["schema"] == "guandan-declaration-rules-v1"
    assert VECTORS["schema"] == "guandan-declaration-golden-vectors-v1"
    assert VECTORS["rules_schema"] == RULES["schema"]
    assert RULES["protocol_version"] == VECTORS["protocol_version"] == PROTOCOL
    assert RULES["ruleset"] == "GD-RULES-0.1"
    assert RULES["authority_source_commit"] == VECTORS["authority_source_commit"] == AUTHORITY_COMMIT
    assert semantic_sha256(RULES) == RULES["integrity"]["semantic_sha256"] == RULES_SEMANTIC
    assert semantic_sha256(VECTORS) == VECTORS["integrity"]["semantic_sha256"] == VECTORS_SEMANTIC
    sums = SHA_PATH.read_text(encoding="utf-8").splitlines()
    assert sums == [
        f"{RULES_SHA}  guance_phase2_declaration_handoff/guandan_declaration_rules_v1.json",
        f"{VECTORS_SHA}  guance_phase2_declaration_handoff/guandan_declaration_golden_vectors_v1.json",
        "5b88c995799ab8b7609b741615038725e4dd6d0e2c26bf8dd2a805cf0a351804  guance_phase2_declaration_handoff/guandan_declaration_rules_v1.md",
        "61cce080052ec4208fd86fac78e847b9a93fab88f9a7ae09a18d2d7ef05823d6  tools/export_guance_declaration_contract.py",
    ]


def test_declaration_fixture_provenance_and_authority_inventory():
    assert PROVENANCE["schema"] == "guance-declaration-fixture-provenance-v1"
    assert PROVENANCE["authority_source_commit"] == AUTHORITY_COMMIT
    assert PROVENANCE["rules_semantic_sha256"] == RULES_SEMANTIC
    assert PROVENANCE["vectors_semantic_sha256"] == VECTORS_SEMANTIC
    assert PROVENANCE["exporter_sha256"] == RULES["exporter_sha256"]
    assert PROVENANCE["handoff_artifact_commit"] is None
    assert PROVENANCE["handoff_artifact_git_status"] == "untracked"
    assert PROVENANCE["copied_files"][RULES_PATH.name]["sha256"] == RULES_SHA
    assert PROVENANCE["copied_files"][VECTORS_PATH.name]["sha256"] == VECTORS_SHA
    assert len(RULES["authority"]["source_files"]) == 16
    assert all(item["git_blob"]["commit"] == AUTHORITY_COMMIT
               and item["matches_head_after_normalization"] is True
               and re.fullmatch(r"[0-9a-f]{40}", item["git_blob"]["oid"])
               for item in RULES["authority"]["source_files"])
    assert RULES["scope_guards"] == {
        "checkpoint_read": False, "complete_real_game_read": False,
        "fixtures": "synthetic physical-card vectors only",
        "guance_repository_accessed": False, "hidden_hand_data_read": False,
        "model_inference_executed": False, "real_capture_read": False,
        "training_executed": False,
    }
    assert PROVENANCE_PATH.name == "provenance.json"


def test_exhaustive_108_card_mapping_and_helper_roundtrip():
    pairs = PROVENANCE["card_mapping_pairs"]
    assert PROVENANCE["card_mapping_schema"] == "guance-guandan-card-mapping-v1"
    assert PROVENANCE["card_mapping_pair_count"] == len(pairs) == 108
    assert PROVENANCE["card_mapping_pairs_sha256"] == "f9fd8795ba14147923c74b2d3f85ea728c2f8ad62aa68f92c7e52f495372fa38"
    assert {item["guandan_card_id"] for item in pairs} == set(range(108))
    assert len({item["guance_physical_code"] for item in pairs}) == 108
    for item in pairs:
        card_id, code = item["guandan_card_id"], item["guance_physical_code"]
        assert guandan_card_id_to_wire(card_id) == code
        assert wire_card_to_guandan_id(code) == card_id
    for rank in range(3, 18):
        assert wire_rank_to_guandan_rank(guandan_rank_to_wire_rank(rank)) == rank
    for suit in range(5):
        assert wire_suit_to_guandan_suit(guandan_suit_to_wire_suit(suit)) == suit


def test_four_kings_mapping_storage_and_action_contract():
    kings = PROVENANCE["four_kings"]
    assert kings == {
        "guandan_card_ids": [52, 53, 106, 107],
        "guance_storage_codes": [14, 270, 15, 271],
        "guance_action_codes": [14, 15, 270, 271],
    }
    assert [guandan_card_id_to_wire(card_id) for card_id in (52, 106, 53, 107)] == kings["guance_storage_codes"]
    assert [guandan_card_id_to_wire(card_id) for card_id in kings["guandan_card_ids"]] == kings["guance_action_codes"]
