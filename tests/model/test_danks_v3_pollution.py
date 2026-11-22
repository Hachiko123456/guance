"""D05 public-information boundary tests for the DanKS V3 path.

- Extra/hidden fields are rejected by the strict decoders (whitelist, never
  read);
- the declared-facts binding rejects declarations that do not match the view
  exactly;
- provider result metadata stays on the whitelisted key set;
- error replies never echo input bytes (no hand cards, no exception text).
"""
from __future__ import annotations

import io
import json

import pytest

from guance.contracts import (AdviceCandidate, CanonicalAdviceResult,
                              CanonicalPublicGameView, CurrentWinningDeclarationFact,
                              DeclaredFacts, ModelContractError, PublicAction,
                              public_model_metadata, validate_advice_for_view)
from guance.infrastructure.model.codec import dumps, envelope
from guance.infrastructure.model.worker import run
from guance.public_state_contract import PublicDeclaration, WildAssignment

LEVEL = 5
OPPONENT_CARDS = (0x35, 0x45, 0x25, 0x39, 0x49)


def _natural() -> PublicDeclaration:
    return PublicDeclaration("FULL_HOUSE", (5, 9), 5, None, 5, OPPONENT_CARDS,
        (WildAssignment(0x25, 5, "HEARTS", False),),
        canonical_action_cards=(0x35, 0x45, 0x25, 0x39, 0x49))


def test_declared_facts_reject_extra_hidden_members():
    facts = {"schema": "Guandan.DeclaredFacts.v1",
             "current_winning_declaration": {"status": "absent"},
             "hidden_opponent_hand": [0x13, 0x19]}
    with pytest.raises(ModelContractError):
        DeclaredFacts.from_dict(facts)
    fact_wire = {"schema": "Guandan.DeclaredFacts.v1",
                 "current_winning_declaration": {"status": "known",
                                                 "declaration": _natural().to_dict(),
                                                 "opponent_private": [0x13]}}
    with pytest.raises(ModelContractError):
        DeclaredFacts.from_dict(fact_wire)


def test_view_decoder_rejects_extra_members():
    raw = CanonicalPublicGameView("t", "g", 1, 0, 0, 0, 2, 5, 0, (0x13,),
                                  (1, 27, 27, 27), (), None, True).to_dict()
    polluted = dict(raw)
    polluted["hidden_opponent_hand"] = [0x19, 0x29]
    with pytest.raises(ModelContractError):
        CanonicalPublicGameView.from_dict(polluted)


def test_binding_rejects_a_declaration_that_does_not_match_the_view():
    lead = PublicAction(1, 1, "play", OPPONENT_CARDS, 5, 3, 0)
    view = CanonicalPublicGameView("t", "g", 4, 3, 3, 1, 1, 5, 0,
        (0x1A, 0x2A, 0x3A, 0x13, 0x23), (27, 22, 27, 5), (lead,), lead, False)
    # A declaration that is valid on its own but bound to different entity
    # cards than the view's lead must fail the binding.
    rogue_cards = (0x25, 0x36, 0x46, 0x39, 0x49)  # H5(wild) C6 D6 C9 D9
    rogue = PublicDeclaration("FULL_HOUSE", (6, 9), 6, None, 5, rogue_cards,
        (WildAssignment(0x25, 6, "CLUBS", True),), ())
    _faces = rogue._effective_faces(rogue.canonical_cards, rogue.wild_assignments)
    object.__setattr__(rogue, "canonical_action_cards",
                       rogue._ordered_cards_for_ranks(_faces, (6, 9)))
    facts = DeclaredFacts(schema="Guandan.DeclaredFacts.v1",
        current_winning_declaration=CurrentWinningDeclarationFact(
            status="known", declaration=rogue))
    with pytest.raises(ModelContractError):
        facts.validate_for_view(view)


def test_result_metadata_stays_on_the_whitelist():
    candidate = AdviceCandidate((0x13,), "single", "单张", 0.5, score_kind="s",
                                metadata={"model_version": "v1",
                                          "prompt": "hidden system prompt",
                                          "hand_snapshot": [0x13]})
    whitelisted = public_model_metadata(candidate.metadata)
    assert whitelisted == {"model_version": "v1"}


def test_worker_error_reply_never_echoes_input_bytes():
    """A polluted observation is rejected at the protocol boundary and the
    error reply carries neither the raw payload bytes nor the hand cards."""
    raw = CanonicalPublicGameView("t", "g", 1, 0, 0, 0, 2, 5, 0, (0x13,),
                                  (1, 27, 27, 27), (), None, True).to_dict()
    polluted = dict(raw)
    polluted["hidden_opponent_hand"] = [0x19, 0x29]

    class Advisor:
        def load(self) -> dict:
            return {}

        def recommend(self, current, request_id):  # pragma: no cover
            raise AssertionError("advisor must not be called for a polluted view")

        def close(self) -> None:
            pass

    class Factory:
        def create(self, source):
            return Advisor()

    ids = {"request_id": "pollution", "session_id": "t", "game_id": "g",
           "state_version": 1, "epoch": 0, "generation": 1}
    messages = [envelope("load", ids, source="controlled-fixture"),
                envelope("recommend", ids, request_kind="public",
                         observation=polluted),
                envelope("stop", ids)]
    output = io.StringIO()
    code = run(input_stream=io.StringIO("".join(dumps(m) for m in messages)),
               output_stream=output, factory=Factory())
    assert code == 3
    wire = output.getvalue()
    assert "hidden_opponent_hand" not in wire
    assert "0x19" not in wire and "25" not in wire.replace('"state_version": 25', "")
    # The advice path was never entered: only protocol + stop statuses exist.
    replies = [json.loads(line) for line in wire.splitlines()]
    assert any(r["type"] == "error" and r.get("fatal") for r in replies)


def test_validate_rejects_non_mapping_candidate_metadata():
    lead = PublicAction(1, 1, "play", (0x18,), 1, 0, 0)
    view = CanonicalPublicGameView("t", "g", 2, 0, 0, 0, 2, 5, 0,
        (0x13, 0x19), (2, 26, 27, 27), (lead,), lead, False)
    with pytest.raises(ModelContractError):
        # The DTO boundary itself rejects non-mapping metadata.
        AdviceCandidate((0x19,), "single", "单张", 0.5, score_kind="s",
                        metadata="not a mapping")  # type: ignore[arg-type]


def test_hand_bounding_is_enforced_by_the_core():
    """The provider slot mapping is hand-bounded; the core's membership gate
    (covered end-to-end in test_danks_v3_core) is the same rule this pollution
    suite relies on: no output card can leave the observer hand."""
    from guance.infrastructure.model.danks_v3.adapter import wire_cards_to_labels
    labels = wire_cards_to_labels((0x13, 0x19, 0x14))
    assert set(labels) == {"S3", "S9", "S4"}
