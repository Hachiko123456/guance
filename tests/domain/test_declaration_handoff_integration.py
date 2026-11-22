from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import re
import traceback

import pytest

from guance.contracts import SourceFrame
from guance.domain.cards import full_deck
from guance.domain.events import ActionEvent, DealEvent, DeclaredActionCommitted, StartEvent
from guance.domain.session import SessionEngine
from guance.domain.state import GameState, StateError
from guance.public_state_contract import (
    CanonicalGuandanPublicState, EvidenceRef, Knowledge, KnowledgeStatus as KS,
    PublicDeclaration, PublicStateContractError, WildAssignment, guandan_card_id_to_wire,
)
from declaration_fixture_oracle import (
    NEGATIVE_VECTORS, POSITIVE_VECTORS, RULES, VECTORS,
    canonical_from_matrix, canonical_sha256, declaration_raw,
    matrix_declaration, negative_declaration, positive_declaration, wire_level,
)

DECK = full_deck()
FAMILIES = ("FULL_HOUSE", "STRAIGHT", "STRAIGHT_FLUSH",
            "PAIR_SEQUENCE", "TRIPLE_SEQUENCE", "FOUR_KINGS")
POSITIVE_BY_FAMILY = tuple(next(vector for vector in POSITIVE_VECTORS
                                if vector["family"] == family)
                           for family in FAMILIES)
PARITY_CASES = tuple((level, case) for level in RULES["level_parity"]["levels"]
                     for case in level["cases"])


def _hand_containing(cards):
    values = list(dict.fromkeys(cards))
    values.extend(card for card in DECK if card not in values)
    return tuple(values[:27])


def _state_for(level, cards):
    state = GameState("declaration-fixture")
    state.apply_deal(DealEvent(-1, level, _hand_containing(cards)))
    state.apply_start(StartEvent(0))
    state.confirm_my_seat_relative("self")
    return state


def _fingerprint(state):
    return (
        state.snapshot(), frozenset(state.hand), tuple(state.remaining),
        tuple(state.history), tuple(state._v2_history), state._v2_winning,
        state.state_version, state.public_state_revision,
        state.public_state_v2("fixture-session"),
    )


def _event_for(declaration, combo=1901):
    return DeclaredActionCommitted(ActionEvent(
        0, "play", declaration.canonical_action_cards, combo_type=combo,
        next_player=3, remaining_after=27 - declaration.length,
    ), declaration)


@pytest.mark.parametrize("vector", POSITIVE_VECTORS, ids=lambda item: item["id"])
def test_all_73_positive_vectors_validate_and_roundtrip(vector):
    declaration = positive_declaration(vector)
    level = wire_level(vector)
    declaration.validate()
    declaration.validate_for_level(level)
    encoded = declaration.to_dict()
    assert PublicDeclaration.from_dict(encoded) == declaration
    canonical = vector["expected"]["canonical_declaration"]
    assert declaration.canonical_cards == tuple(
        guandan_card_id_to_wire(card_id) for card_id in canonical["declaration_card_ids"])
    assert declaration.canonical_action_cards == tuple(
        guandan_card_id_to_wire(card_id) for card_id in canonical["canonical_action_card_ids"])
    assert declaration.protocol_length == canonical["declared_length"]
    assert declaration.length == len(vector["physical_cards"])


@pytest.mark.parametrize("vector", POSITIVE_BY_FAMILY, ids=lambda item: item["family"])
def test_six_complex_families_become_known_in_game_state_without_changing_v1(vector):
    declaration = positive_declaration(vector)
    level = wire_level(vector)
    observed_wire_order = tuple(guandan_card_id_to_wire(item["card_id"])
                                for item in vector["physical_cards"])
    combo = 2000 + FAMILIES.index(vector["family"])
    state = _state_for(level, observed_wire_order)
    event = DeclaredActionCommitted(ActionEvent(
        0, "play", observed_wire_order, combo_type=combo, next_player=3,
        remaining_after=27 - len(observed_wire_order),
    ), declaration)
    state.apply_declared_action(event)
    view = state.public_state_v2("fixture-session")
    assert state.history[0].cards == observed_wire_order
    assert state.history[0].combo_type == combo
    assert view.history.actions[0].cards == declaration.canonical_cards
    assert view.history.actions[0].declaration.value == declaration
    assert view.current_winning.value.cards == declaration.canonical_cards
    assert view.current_winning.value.declaration.value == declaration
    assert declaration.canonical_action_cards == tuple(
        guandan_card_id_to_wire(card_id)
        for card_id in vector["expected"]["canonical_declaration"]["canonical_action_card_ids"])
    assert CanonicalGuandanPublicState.from_dict(view.to_dict()) == view


@pytest.mark.parametrize("level,case", PARITY_CASES,
                         ids=lambda value: value["id"] if "id" in value else value["level"]["name"])
def test_338_level_parity_cases_reconstruct_generic_canonical_hash(level, case):
    canonical = canonical_from_matrix(level, case)
    assert canonical_sha256(canonical) == case["canonical_declaration_sha256"]
    assert re.fullmatch(r"[0-9a-f]{64}", case["complete_token_sequence_sha256"])
    assert re.fullmatch(r"[0-9a-f]{64}", case["marvel_action_rows_sha256"])
    assert case["passed"] is True
    assert all(case["checks"].values())
    declaration = matrix_declaration(level, case)
    declaration.validate_for_level(wire_level(level))
    assert PublicDeclaration.from_dict(declaration.to_dict()) == declaration


def test_338_level_parity_coverage_summary_and_boundaries():
    parity = RULES["level_parity"]
    assert parity["matrix_case_count"] == len(PARITY_CASES) == 13 * 26 == 338
    assert parity["cases_per_level"] == 26
    assert [item["level"]["value"] for item in parity["levels"]] == list(range(3, 16))
    assert set(parity["families_tested"]) == set(FAMILIES)
    assert all(item["case_count"] == len(item["cases"]) == 26 and item["passed"]
               for item in parity["levels"])
    case_types = {case["case_type"] for _level, case in PARITY_CASES}
    assert {"matrix_natural", "matrix_wild_natural", "matrix_wild_substituted",
            "matrix_wild_substituted_level_rank"} <= case_types
    assert any(case["wildcard_count"] == 2 for _level, case in PARITY_CASES)
    assert set(VECTORS["special_boundary_vector_ids"]) <= {
        vector["id"] for vector in VECTORS["vectors"]}
    assert parity["invalid_levels_rejected"] == ["SMALL_JOKER", "BIG_JOKER"]


@pytest.mark.parametrize("vector", NEGATIVE_VECTORS, ids=lambda item: item["id"])
def test_all_50_negative_candidates_fail_level_aware_dto(vector):
    declaration = negative_declaration(vector)
    with pytest.raises(PublicStateContractError) as caught:
        declaration.validate_for_level(wire_level(vector))
    assert caught.value.args == ("PUBLIC_STATE_INVALID",)
    assert vector["expected"]["reason_code"] not in str(caught.value)
    raw = declaration_raw(declaration)
    try:
        decoded = PublicDeclaration.from_dict(raw)
    except PublicStateContractError as raw_error:
        assert raw_error.args == ("PUBLIC_STATE_INVALID",)
    else:
        with pytest.raises(PublicStateContractError, match="PUBLIC_STATE_INVALID"):
            decoded.validate_for_level(wire_level(vector))


@pytest.mark.parametrize("vector", NEGATIVE_VECTORS, ids=lambda item: item["id"])
def test_all_50_negative_candidates_fail_event_or_state_atomically(vector):
    declaration = negative_declaration(vector)
    event = _event_for(declaration)
    state = _state_for(wire_level(vector), declaration.canonical_action_cards)
    before = _fingerprint(state)
    structurally_invalid = False
    try:
        declaration.validate()
    except PublicStateContractError:
        structurally_invalid = True
    if structurally_invalid:
        with pytest.raises(PublicStateContractError, match="SEMANTIC_EVENT_INVALID"):
            event.validate()
    else:
        event.validate()  # level-dependent invalidity is intentionally deferred to GameState.
    assert _fingerprint(state) == before
    with pytest.raises(StateError, match="declared action semantic invalid"):
        state.apply_declared_action(event)
    assert _fingerprint(state) == before


@pytest.mark.parametrize("vector", NEGATIVE_VECTORS, ids=lambda item: item["id"])
def test_all_50_negative_candidates_pause_session_and_clear_projection(vector):
    declaration = negative_declaration(vector)
    level = wire_level(vector)
    hand = _hand_containing(declaration.canonical_action_cards)

    def parse(payload, meta):
        if payload == b"deal":
            return (DealEvent(-1, level, hand, meta=meta),)
        if payload == b"start":
            return (StartEvent(0, meta=meta),)
        if payload == b"candidate":
            return (replace(_event_for(declaration), action=replace(
                _event_for(declaration).action, meta=meta)),)
        raise ValueError("PRIVATE_NEGATIVE_VECTOR_SENTINEL")

    engine = SessionEngine("fixture-session", aggregate_parser=parse,
                           accepted_host="fixture-host")
    def feed(payload, sequence):
        return engine.feed(SourceFrame("fixture-host", payload, float(sequence),
                                       "fixture-connection", sequence, "server"))
    feed(b"deal", 0); feed(b"start", 1); engine.confirm_my_seat_relative("self")
    state = engine.state
    before = _fingerprint(state)
    updates = feed(b"candidate", 2)
    assert [update.kind for update in updates] == ["error"]
    assert updates[0].error_code == "state_invalid"
    assert updates[0].message == "declared action semantic invalid"
    assert engine.latest_public_state_v2 is None and engine.latest_observation is None
    assert engine.paused and _fingerprint(state) == before
    assert vector["id"] not in json.dumps([update.message for update in updates])


def test_schema_revision_missing_wrong_and_old_rank_encoding_rejected():
    state = _state_for(5, ())
    raw = state.public_state_v2("fixture-session").to_dict()
    assert raw["schema_revision"] == 1
    missing = dict(raw); del missing["schema_revision"]
    with pytest.raises(PublicStateContractError, match="PUBLIC_STATE_UNSUPPORTED"):
        CanonicalGuandanPublicState.from_dict(missing)
    for revision in (0, 2, True, 1.0):
        wrong = dict(raw); wrong["schema_revision"] = revision
        with pytest.raises(PublicStateContractError):
            CanonicalGuandanPublicState.from_dict(wrong)

    declaration = positive_declaration(POSITIVE_VECTORS[0])
    old = declaration_raw(declaration)
    old["rank_encoding"] = "guance-wire-rank-v0"
    with pytest.raises(PublicStateContractError, match="PUBLIC_STATE_UNSUPPORTED"):
        PublicDeclaration.from_dict(old)
    old = declaration_raw(declaration)
    old["declared_ranks"] = [1]
    with pytest.raises(PublicStateContractError):
        PublicDeclaration.from_dict(old)


def test_four_kings_storage_action_and_protocol_length_are_lossless():
    vector = next(item for item in POSITIVE_VECTORS if item["family"] == "FOUR_KINGS")
    declaration = positive_declaration(vector)
    assert declaration.canonical_cards == (14, 270, 15, 271)
    assert declaration.canonical_action_cards == (14, 15, 270, 271)
    assert declaration.protocol_length is None
    assert declaration.declared_ranks == () and declaration.comparison_rank == 17
    declaration.validate_for_level(wire_level(vector))



def test_vector_inventory_reason_codes_families_and_special_boundaries():
    assert VECTORS["vector_count"] == {
        "base_negative": 48, "base_positive": 32, "negative": 50,
        "positive": 73, "special_boundary": 43, "total": 123,
    }
    assert len(POSITIVE_VECTORS) == 73 and len(NEGATIVE_VECTORS) == 50
    ids = [vector["id"] for vector in VECTORS["vectors"]]
    assert len(ids) == len(set(ids)) == 123
    assert {vector["family"] for vector in VECTORS["vectors"]} == set(FAMILIES)
    assert all(vector["expected"]["reason_code"] for vector in NEGATIVE_VECTORS)
    coverage = {tag for vector in VECTORS["vectors"] for tag in vector["coverage"]}
    assert {"special_level_THREE", "special_level_ACE", "special_level_TWO",
            "wildcard_natural", "wildcard_substituted", "wildcard_max_count"} <= coverage


def test_public_state_v2_consumers_are_limited_to_explicit_phase7_subset_paths():
    root = __import__("pathlib").Path(__file__).parents[2] / "src" / "guance"
    allowed = {
        root / "infrastructure" / "capture" / "live_subset.py",
        root / "infrastructure" / "capture" / "canonical_capture.py",
        root / "infrastructure" / "model" / "marvel_v2" / "rules_oracle.py",
        root / "infrastructure" / "model" / "marvel_v2" / "live_adapter.py",
        # Phase 3 explicit provider-boundary consumers (bounded wire projection
        # and real-checkpoint recommendation; no provider/tensor logic leaks).
        root / "infrastructure" / "model" / "marvel_v2" / "decision_request.py",
        root / "infrastructure" / "model" / "marvel_v2" / "recommendation.py",
        # DanKS V3 phase 3 explicit provider-boundary consumer: declaration to
        # follow-target conversion and the legal-candidate formation point;
        # no provider/tensor logic leaks beyond this adapter.
        root / "infrastructure" / "model" / "danks_v3" / "adapter.py",
        # DanKS V3 phase 5 controller projection: verbatim Knowledge-to-
        # DeclaredFacts translation for the IPC request; no state mutation.
        root / "application" / "controller.py",
    }
    for directory in (root / "infrastructure" / "model", root / "application"):
        for path in directory.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if any(name in text for name in ("CanonicalGuandanPublicState", "PublicDeclaration")):
                assert path in allowed, path
            assert "latest_public_state_v2" not in text


# GMV2-S2-DECL003-FINAL-REPAIR-20261012: strict order and declaration coherence.
def _assert_bounded_rejection(operation, message="PUBLIC_STATE_INVALID",
                              error_type=PublicStateContractError):
    with pytest.raises(error_type) as caught:
        operation()
    error = caught.value
    assert error.args == (message,)
    assert str(error) == message
    assert error.__cause__ is None
    rendered = "".join(traceback.format_exception(error))
    assert "PRIVATE_EMPTY_ORDER_SENTINEL" not in rendered
    assert "TypeError:" not in rendered and "KeyError:" not in rendered


def _known_view(declaration, level=5, after_pass=False):
    state = _state_for(level, declaration.canonical_action_cards)
    state.apply_declared_action(_event_for(declaration))
    if after_pass:
        state.apply_action(ActionEvent(3, "pass", next_player=2, reset_flag=0))
    view = state.public_state_v2("fixture-session")
    view.validate()
    assert CanonicalGuandanPublicState.from_dict(view.to_dict()) == view
    return view


def _with_declaration_facts(view, *, history=None, winning=None):
    if history is not None:
        actions = list(view.history.actions)
        last_play = max(index for index, action in enumerate(actions) if action.kind == "play")
        actions[last_play] = replace(actions[last_play], declaration=history)
        view = replace(view, history=replace(view.history, actions=tuple(actions)))
    if winning is not None:
        view = replace(view, current_winning=replace(
            view.current_winning,
            value=replace(view.current_winning.value, declaration=winning),
        ))
    return view


def _raw_declaration_facts(raw):
    last_play = next(action for action in reversed(raw["history"]["actions"])
                     if action["kind"] == "play")
    return last_play["declaration"], raw["current_winning"]["value"]["declaration"]


@pytest.mark.parametrize("vector", POSITIVE_VECTORS, ids=lambda item: item["id"])
def test_all_73_positive_raw_payloads_reject_empty_action_order(vector):
    valid = positive_declaration(vector)
    valid.validate_for_level(wire_level(vector))
    raw = valid.to_dict()
    assert PublicDeclaration.from_dict(raw) == valid
    raw["canonical_action_cards"] = []
    original_raw = deepcopy(raw)
    _assert_bounded_rejection(lambda: PublicDeclaration.from_dict(raw))
    assert raw == original_raw  # The decoder must not normalize the supplied payload.

    empty = replace(valid, canonical_action_cards=())
    assert empty.canonical_action_cards == ()  # No constructor auto-fill either.
    for operation in (empty.validate, empty.to_dict,
                      lambda: empty.validate_for_level(wire_level(vector))):
        _assert_bounded_rejection(operation)
    assert empty.canonical_action_cards == ()
    assert valid.canonical_action_cards


@pytest.mark.parametrize("vector", POSITIVE_VECTORS, ids=lambda item: item["id"])
def test_all_73_empty_action_orders_fail_event_and_state_atomically(vector):
    valid = positive_declaration(vector)
    valid.validate_for_level(wire_level(vector))
    empty = replace(valid, canonical_action_cards=())
    # Keep the observed ActionEvent valid/nonempty: the missing declaration
    # metadata must be the rejection cause, not an empty physical play.
    event = replace(_event_for(valid), declaration=empty)
    assert event.action.cards == valid.canonical_action_cards
    state = _state_for(wire_level(vector), event.action.cards)
    before = _fingerprint(state)
    _assert_bounded_rejection(event.validate, "SEMANTIC_EVENT_INVALID")
    assert _fingerprint(state) == before
    _assert_bounded_rejection(
        lambda: state.apply_declared_action(event),
        "declared action semantic invalid", StateError,
    )
    assert _fingerprint(state) == before
    view = state.public_state_v2("fixture-session")
    assert view.history.actions == ()
    assert view.current_winning.status is KS.ABSENT


@pytest.mark.parametrize("location", ["history-only", "winning-only", "both"])
@pytest.mark.parametrize("vector", POSITIVE_VECTORS, ids=lambda item: item["id"])
def test_all_73_empty_action_orders_fail_top_level_boundaries(vector, location):
    valid = positive_declaration(vector)
    view = _known_view(valid, wire_level(vector))
    raw = view.to_dict()  # Serialize only the valid state, then corrupt raw input.
    history_raw, winning_raw = _raw_declaration_facts(raw)
    assert history_raw["status"] == winning_raw["status"] == "known"
    empty = replace(valid, canonical_action_cards=())
    history_fact = winning_fact = None
    if location in ("history-only", "both"):
        history_raw["value"]["canonical_action_cards"] = []
        history_fact = replace(view.history.actions[-1].declaration, value=empty)
    if location in ("winning-only", "both"):
        winning_raw["value"]["canonical_action_cards"] = []
        winning_fact = replace(view.current_winning.value.declaration, value=empty)
    candidate = _with_declaration_facts(view, history=history_fact, winning=winning_fact)
    for operation in (candidate.validate, candidate.to_dict):
        _assert_bounded_rejection(operation)
    original_raw = deepcopy(raw)
    _assert_bounded_rejection(lambda: CanonicalGuandanPublicState.from_dict(raw))
    assert raw == original_raw
    assert CanonicalGuandanPublicState.from_dict(view.to_dict()) == view


@pytest.mark.parametrize("vector", POSITIVE_BY_FAMILY, ids=lambda item: item["family"])
def test_empty_action_order_pauses_session_without_committing_known(vector):
    valid = positive_declaration(vector)
    level = wire_level(vector)
    valid.validate_for_level(level)
    empty = replace(valid, canonical_action_cards=())
    event = replace(_event_for(valid), declaration=empty)
    hand = _hand_containing(event.action.cards)

    def parse(payload, meta):
        if payload == b"deal":
            return (DealEvent(-1, level, hand, meta=meta),)
        if payload == b"start":
            return (StartEvent(0, meta=meta),)
        if payload == b"empty-order":
            return (replace(event, action=replace(event.action, meta=meta)),)
        raise ValueError("PRIVATE_EMPTY_ORDER_SENTINEL")

    engine = SessionEngine("fixture-session", aggregate_parser=parse,
                           accepted_host="fixture-host")

    def feed(payload, sequence):
        return engine.feed(SourceFrame("fixture-host", payload, float(sequence),
                                       "fixture-connection", sequence, "server"))

    feed(b"deal", 0)
    feed(b"start", 1)
    engine.confirm_my_seat_relative("self")
    assert engine.latest_public_state_v2 is not None
    assert engine.latest_observation is not None
    before = _fingerprint(engine.state)
    updates = feed(b"empty-order", 2)
    assert [update.kind for update in updates] == ["error"]
    assert updates[0].error_code == "state_invalid"
    assert updates[0].message == "declared action semantic invalid"
    assert engine.paused
    assert engine.latest_public_state_v2 is None and engine.latest_observation is None
    assert _fingerprint(engine.state) == before
    assert engine.state.public_state_v2("fixture-session").history.actions == ()
    assert "PRIVATE_EMPTY_ORDER_SENTINEL" not in repr(updates)


def _ambiguous_full_house_declarations():
    # The same five physical cards admit two individually valid level-5
    # interpretations. Storage order is identical, but the committed triple,
    # comparison rank, wildcard interpretation and action order are different.
    cards = (0x35, 0x45, 0x25, 0x39, 0x49)
    triple_five = PublicDeclaration(
        "FULL_HOUSE", (5, 9), 5, None, 5, cards,
        (WildAssignment(0x25, 5, "HEARTS", False),),
        canonical_action_cards=(0x35, 0x45, 0x25, 0x39, 0x49),
    )
    triple_nine = PublicDeclaration(
        "FULL_HOUSE", (9, 5), 9, None, 5, cards,
        (WildAssignment(0x25, 9, "CLUBS", True),),
        canonical_action_cards=(0x25, 0x39, 0x49, 0x35, 0x45),
    )
    for declaration in (triple_five, triple_nine):
        declaration.validate_for_level(5)
        assert PublicDeclaration.from_dict(declaration.to_dict()) == declaration
    assert triple_five.canonical_cards == triple_nine.canonical_cards
    assert triple_five.declared_ranks != triple_nine.declared_ranks
    assert triple_five.comparison_rank != triple_nine.comparison_rank
    assert triple_five.wild_assignments != triple_nine.wild_assignments
    assert triple_five.canonical_action_cards != triple_nine.canonical_action_cards
    return triple_five, triple_nine


@pytest.mark.parametrize("conflict", ["triple-rank", "wildcard-mode"])
@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
@pytest.mark.parametrize("after_pass", [False, True], ids=["after-play", "after-pass"])
def test_conflicting_known_declarations_for_same_action_fail_top_level(conflict, reverse, after_pass):
    committed, conflicting = _ambiguous_full_house_declarations()
    if conflict == "wildcard-mode":
        # Same ranks/comparison/order, different explicit natural/substituted
        # interpretation: checking only family and rank is not enough.
        conflicting = replace(committed, wild_assignments=(
            WildAssignment(0x25, 5, "CLUBS", True),
        ))
        conflicting.validate_for_level(5)
        assert committed.declared_ranks == conflicting.declared_ranks
        assert committed.comparison_rank == conflicting.comparison_rank
        assert committed.canonical_action_cards == conflicting.canonical_action_cards
        assert committed.wild_assignments != conflicting.wild_assignments
    if reverse:
        committed, conflicting = conflicting, committed
    view = _known_view(committed, after_pass=after_pass)
    # Establish that the other declaration is also a valid complete state,
    # not merely a locally valid object rejected for some unrelated field.
    _known_view(conflicting, after_pass=after_pass)
    last_play = next(action for action in reversed(view.history.actions) if action.kind == "play")
    assert last_play.actor == view.current_winning.value.actor
    assert last_play.cards == view.current_winning.value.cards == committed.canonical_cards
    candidate = _with_declaration_facts(view, winning=replace(
        view.current_winning.value.declaration, value=conflicting,
    ))
    assert last_play.declaration.status is KS.KNOWN
    assert candidate.current_winning.value.declaration.status is KS.KNOWN
    for operation in (candidate.validate, candidate.to_dict):
        _assert_bounded_rejection(operation)

    raw = view.to_dict()
    _history, winning = _raw_declaration_facts(raw)
    winning["value"] = conflicting.to_dict()
    original_raw = deepcopy(raw)
    _assert_bounded_rejection(lambda: CanonicalGuandanPublicState.from_dict(raw))
    assert raw == original_raw
    assert CanonicalGuandanPublicState.from_dict(view.to_dict()) == view


@pytest.mark.parametrize("partial_side", ["history", "winning"])
@pytest.mark.parametrize("partial_status", [KS.UNSUPPORTED, KS.UNKNOWN], ids=lambda item: item.value)
@pytest.mark.parametrize("reverse", [False, True], ids=["triple-five", "triple-nine"])
@pytest.mark.parametrize("after_pass", [False, True], ids=["after-play", "after-pass"])
def test_one_partial_declaration_side_remains_allowed(partial_side, partial_status, reverse, after_pass):
    declarations = _ambiguous_full_house_declarations()
    known = declarations[int(reverse)]
    view = _known_view(declarations[0], after_pass=after_pass)
    partial = Knowledge(partial_status, reason="declaration_not_observed")
    known_fact = Knowledge.known(known, EvidenceRef("synthetic.declaration"))
    candidate = _with_declaration_facts(
        view,
        history=partial if partial_side == "history" else known_fact,
        winning=partial if partial_side == "winning" else known_fact,
    )
    candidate.validate()
    raw = candidate.to_dict()
    decoded = CanonicalGuandanPublicState.from_dict(raw)
    assert decoded == candidate
    history_raw, winning_raw = _raw_declaration_facts(raw)
    partial_raw, known_raw = ((history_raw, winning_raw) if partial_side == "history"
                              else (winning_raw, history_raw))
    assert partial_raw == {"status": partial_status.value, "reason": "declaration_not_observed"}
    assert known_raw["status"] == "known"
    assert known_raw["value"] == known.to_dict()


@pytest.mark.parametrize("after_pass", [False, True], ids=["after-play", "after-pass"])
def test_identical_known_declarations_allow_distinct_evidence(after_pass):
    declaration, _other = _ambiguous_full_house_declarations()
    view = _known_view(declaration, after_pass=after_pass)
    history_fact = Knowledge.known(
        declaration, EvidenceRef("synthetic.committed", sequence=2, derivation="observed_action"))
    winning_fact = Knowledge.known(
        PublicDeclaration.from_dict(declaration.to_dict()),
        EvidenceRef("synthetic.winning", sequence=3, derivation="winning_projection"))
    assert history_fact.value == winning_fact.value
    assert history_fact.evidence != winning_fact.evidence
    candidate = _with_declaration_facts(view, history=history_fact, winning=winning_fact)
    candidate.validate()
    assert CanonicalGuandanPublicState.from_dict(candidate.to_dict()) == candidate
