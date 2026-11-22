"""Declaration-fact transport tests through client, JSONL and worker (D03).

Phase 1 proved the three declaration facts collapse into one identical
worker input (CanonicalPublicGameView.v1 drops declarations).  Phase 3 fixes
this: ``PublicRecommendationRequestV2`` + ``Guandan.DeclaredFacts.v1`` carry
the facts through the wire and the worker hands them to opt-in advisors.
Legacy advisors and legacy envelopes keep byte-identical behavior.
"""
from __future__ import annotations

from dataclasses import replace
import io
import json

from guance.contracts import (
    CurrentWinningDeclarationFact, DeclaredFacts, ModelContractError,
    PublicRecommendationRequest, PublicRecommendationRequestV2, RequestIdentity,
)
from guance.domain.cards import full_deck
from guance.domain.events import ActionEvent, DealEvent, DeclaredActionCommitted, StartEvent
from guance.domain.session import SessionEngine
from guance.infrastructure.model.client import InferenceClient
from guance.infrastructure.model.codec import dumps, envelope
from guance.infrastructure.model.worker import run
from guance.public_state_contract import PublicDeclaration, WildAssignment

LEVEL = 5
OPPONENT_CARDS = (0x35, 0x45, 0x25, 0x39, 0x49)  # C5 D5 H5 C9 D9


def serialized(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def natural_declaration() -> PublicDeclaration:
    return PublicDeclaration("FULL_HOUSE", (5, 9), 5, None, 5, OPPONENT_CARDS,
        (WildAssignment(0x25, 5, "HEARTS", False),),
        canonical_action_cards=(0x35, 0x45, 0x25, 0x39, 0x49))


def club_substituted_declaration() -> PublicDeclaration:
    return PublicDeclaration("FULL_HOUSE", (5, 9), 5, None, 5, OPPONENT_CARDS,
        (WildAssignment(0x25, 5, "CLUBS", True),),
        canonical_action_cards=(0x35, 0x45, 0x25, 0x39, 0x49))


OBSERVER_ACTION_CARDS = (0x1A, 0x2A, 0x3A, 0x13, 0x23)  # ST HT CT S3 H3


def observer_hand() -> tuple[int, ...]:
    """Deterministic 27-card observer hand containing the five fixed cards."""
    import random
    rng = random.Random(20261120)
    pool = [code for code in full_deck()
            if code not in {*OPPONENT_CARDS, 0x25, 0x125,
                            *OBSERVER_ACTION_CARDS}]
    return tuple(OBSERVER_ACTION_CARDS) + tuple(rng.sample(pool, 22))


def build_chain(declaration: PublicDeclaration | None, session_id: str) -> SessionEngine:
    rng_pool = list(observer_hand())
    action = ActionEvent(0, "play", OPPONENT_CARDS, combo_type=5, next_player=3,
        remaining_after=22, action_index=1, expected_trick=0)
    event = action if declaration is None else DeclaredActionCommitted(action, declaration)
    events = [DealEvent(-1, LEVEL, tuple(rng_pool)), StartEvent(0), event]

    def parse(payload, meta):
        value = events[int(payload)]
        if isinstance(value, DeclaredActionCommitted):
            return (replace(value, action=replace(value.action, meta=meta)),)
        return (replace(value, meta=meta),)

    engine = SessionEngine(session_id, aggregate_parser=parse,
                           accepted_host="synthetic-host")
    for position in range(3):
        updates = engine.feed(_source_frame(position))
        assert not any(update.kind == "error" for update in updates)
        if position == 1:
            updates = engine.confirm_my_seat_relative("left")
            assert not any(update.kind == "error" for update in updates)
    assert not engine.paused and engine.latest_observation is not None
    return engine


def _source_frame(position):
    from guance.contracts import SourceFrame
    return SourceFrame("synthetic-host", str(position).encode(), float(position),
                       "synthetic-connection", position, "server")


class RecordingAdvisor:
    """Fixture advisor opting into DeclaredFacts.v1 and recording inputs."""

    supports_declared_facts = True

    def __init__(self) -> None:
        self.received: list[tuple[dict, dict | None]] = []

    def load(self) -> dict:
        return {}

    def recommend(self, view, request_id, declared_facts=None):
        self.received.append((view.to_dict(),
                              declared_facts.to_dict() if declared_facts is not None
                              else None))
        from guance.contracts import AdviceCandidate, CanonicalAdviceResult
        return CanonicalAdviceResult(*view.key, request_id, provider="fixture",
            model_version="v1", score_kind="test_score",
            candidates=(AdviceCandidate((), "pass", "pass", 0.5,
                                        score_kind="test_score"),))

    def close(self) -> None:
        pass


class RecordingFactory:
    def __init__(self, advisor) -> None:
        self._advisor = advisor

    def create(self, source):
        return self._advisor


def _worker_recommend(engine: SessionEngine, advisor) -> dict:
    from guance.contracts import CanonicalPublicGameView
    view = CanonicalPublicGameView.from_observation(engine.latest_observation)
    raw = view.to_dict()
    winning = engine.latest_public_state_v2.current_winning
    fact = winning.value.declaration
    if fact.status.value == "known":
        winning_fact = CurrentWinningDeclarationFact(status="known",
                                                     declaration=fact.value)
    else:
        winning_fact = CurrentWinningDeclarationFact(
            status="unsupported", reason=fact.reason)
    facts = DeclaredFacts(schema="Guandan.DeclaredFacts.v2",
                          current_winning_declaration=winning_fact)
    ids = {"request_id": "ipc-request", "session_id": view.session_id,
        "game_id": view.game_id, "state_version": view.state_version,
        "epoch": 0, "generation": 1}
    messages = [envelope("load", ids, source="controlled-fixture"),
        envelope("recommend", ids, request_kind="public", observation=raw,
                 declared_facts=facts.to_dict()),
        envelope("stop", ids)]
    output = io.StringIO()
    code = run(input_stream=io.StringIO("".join(dumps(m) for m in messages)),
               output_stream=output, factory=RecordingFactory(advisor))
    assert code == 0
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    return {"view_raw": raw, "facts": facts, "replies": replies,
            "winning_fact": winning_fact}


def test_three_declaration_facts_reach_the_worker_distinctly():
    advisor = RecordingAdvisor()
    hashes = []
    for label, declaration in (("natural", natural_declaration()),
                               ("club_sub", club_substituted_declaration()),
                               ("missing", None)):
        engine = build_chain(declaration, "phase3-ipc")
        outcome = _worker_recommend(engine, advisor)
        result = next(r for r in outcome["replies"] if r["type"] in {"result", "error"})
        assert result["type"] == "result", result
        assert advisor.received[-1][1] is not None
        assert advisor.received[-1][1]["schema"] == "Guandan.DeclaredFacts.v2"
        hashes.append(serialized(advisor.received[-1]))
    # The phase 1 loss is fixed: three declaration facts -> three distinct
    # worker inputs.
    assert len(set(hashes)) == 3


def test_worker_facts_match_the_state_declaration_exactly():
    advisor = RecordingAdvisor()
    engine = build_chain(club_substituted_declaration(), "phase3-ipc-known")
    outcome = _worker_recommend(engine, advisor)
    received = advisor.received[-1][1]
    declaration = received["current_winning_declaration"]["declaration"]
    assert declaration["family"] == "FULL_HOUSE"
    assert declaration["comparison_rank"] == 5
    assert declaration["wild_assignments"][0]["assigned_suit"] == "CLUBS"
    assert declaration["wild_assignments"][0]["substituted"] is True
    assert declaration["canonical_cards"] == list(OPPONENT_CARDS)
    missing = build_chain(None, "phase3-ipc-missing")
    outcome = _worker_recommend(missing, advisor)
    received = advisor.received[-1][1]
    assert received["current_winning_declaration"]["status"] == "unsupported"
    assert received["current_winning_declaration"].get("declaration") is None


def test_legacy_advisor_keeps_byte_identical_behavior():
    class LegacyAdvisor:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def load(self) -> dict:
            return {}

        def recommend(self, view, request_id):
            self.calls.append((view.to_dict(), request_id))
            from guance.contracts import AdviceCandidate, CanonicalAdviceResult
            return CanonicalAdviceResult(*view.key, request_id, provider="legacy",
                model_version="v1", score_kind="test_score",
                candidates=(AdviceCandidate((), "pass", "pass", 0.5,
                                            score_kind="test_score"),))

        def close(self) -> None:
            pass

    advisor = LegacyAdvisor()
    engine = build_chain(natural_declaration(), "phase3-ipc-legacy")
    outcome = _worker_recommend(engine, advisor)
    result = next(r for r in outcome["replies"] if r["type"] in {"result", "error"})
    assert result["type"] == "result"
    assert len(advisor.calls) == 1  # 2-arg call, no facts argument


def test_worker_rejects_malformed_and_unbound_declared_facts():
    advisor = RecordingAdvisor()
    engine = build_chain(natural_declaration(), "phase3-ipc-bad")
    view_raw = _worker_recommend(engine, RecordingAdvisor())["view_raw"]
    ids = {"request_id": "bad-facts", "session_id": view_raw["session_id"],
        "game_id": view_raw["game_id"], "state_version": view_raw["state_version"],
        "epoch": 0, "generation": 1}
    bad_schema = {"schema": "Guandan.DeclaredFacts.v9",
                  "current_winning_declaration": {"status": "known"}}
    # A declaration bound to a different entity-card set cannot validate.
    broken = dict(natural_declaration().to_dict())
    broken["canonical_cards"] = [0x36, 0x45, 0x25, 0x39, 0x49]
    bad_binding = {"schema": "Guandan.DeclaredFacts.v2",
                   "current_winning_declaration": {"status": "known",
                                                   "declaration": broken}}
    for facts in (bad_schema, bad_binding):
        messages = [envelope("load", ids, source="controlled-fixture"),
            envelope("recommend", ids, request_kind="public",
                     observation=view_raw, declared_facts=facts),
            envelope("stop", ids)]
        output = io.StringIO()
        code = run(input_stream=io.StringIO("".join(dumps(m) for m in messages)),
                   output_stream=output, factory=RecordingFactory(advisor))
        assert code == 3  # protocol-level rejection, engine stops fail-closed
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        assert any(r["type"] == "error" for r in replies)


def test_client_submits_declared_facts_in_the_wire_body():
    class SubmissionOnlyClient(InferenceClient):
        def start(self) -> None:
            pass

    engine = build_chain(club_substituted_declaration(), "phase3-ipc-client")
    from guance.contracts import CanonicalPublicGameView
    view = CanonicalPublicGameView.from_observation(engine.latest_observation)
    winning = engine.latest_public_state_v2.current_winning
    fact = winning.value.declaration
    facts = DeclaredFacts(schema="Guandan.DeclaredFacts.v2",
        current_winning_declaration=CurrentWinningDeclarationFact(
            status="known", declaration=fact.value))
    statuses: list[dict] = []
    client = SubmissionOnlyClient("unloaded-fixture.npz",
                                  on_result=lambda result: None,
                                  on_status=statuses.append)
    identity = RequestIdentity("req-v2", view.session_id, view.game_id,
                               view.state_version)
    request = PublicRecommendationRequestV2(identity, view, facts)
    try:
        request_id = client.submit(request)
        queued = client._requests.get_nowait()
        assert request_id == queued[1]["request_id"]
        submitted = queued[0]
        assert type(submitted) is PublicRecommendationRequestV2
        # The wire body is built from the queued request exactly like
        # client._run does (getattr-based member addition).
        body = {"request_kind": "public", "observation": submitted.view.to_dict()}
        if getattr(submitted, "declared_facts", None) is not None:
            body["declared_facts"] = submitted.declared_facts.to_dict()
        assert body["declared_facts"]["schema"] == "Guandan.DeclaredFacts.v2"
        assert body["declared_facts"]["current_winning_declaration"][
            "declaration"]["wild_assignments"][0]["assigned_suit"] == "CLUBS"
        # Legacy v1 request: no declared_facts member on the wire.
        legacy = PublicRecommendationRequest(identity, view)
        request_id = client.submit(legacy)
        queued = client._requests.get_nowait()
        assert type(queued[0]) is PublicRecommendationRequest
        assert getattr(queued[0], "declared_facts", None) is None
    finally:
        client.close()


def test_client_validates_result_against_declared_target():
    """The client-side shared validation now also sees the declaration: a
    candidate that beats the declared target passes, one that does not is
    rejected before delivery."""
    from guance.contracts import (AdviceCandidate, CanonicalAdviceResult,
                                  validate_advice_for_view)
    engine = build_chain(_nine_substituted(), "phase3-ipc-validate")
    from guance.contracts import CanonicalPublicGameView
    view = CanonicalPublicGameView.from_observation(engine.latest_observation)
    facts = DeclaredFacts(schema="Guandan.DeclaredFacts.v2",
        current_winning_declaration=CurrentWinningDeclarationFact(
            status="known", declaration=_nine_substituted()))
    triple_ten = OBSERVER_ACTION_CARDS  # ST HT CT S3 H3, inside the hand
    result = CanonicalAdviceResult(*view.key, "req", provider="p", model_version="v",
        score_kind="s", candidates=(AdviceCandidate(triple_ten, "full", "三带二",
                                                    0.5, score_kind="s"),))
    # Comparison rank 9: triple-10 full house beats it.
    validate_advice_for_view(view, result, declared_facts=facts)
    natural = DeclaredFacts(schema="Guandan.DeclaredFacts.v2",
        current_winning_declaration=CurrentWinningDeclarationFact(
            status="known", declaration=natural_declaration()))
    try:
        validate_advice_for_view(view, result, declared_facts=natural)
        raise AssertionError("comp-5 target unexpectedly accepted a comp-10 follow")
    except ModelContractError as exc:
        assert exc.code == "MODEL_OUTPUT_NOT_BEATING"


def _nine_substituted() -> PublicDeclaration:
    return PublicDeclaration("FULL_HOUSE", (9, 5), 9, None, 5, OPPONENT_CARDS,
        (WildAssignment(0x25, 9, "CLUBS", True),),
        canonical_action_cards=(0x25, 0x39, 0x49, 0x35, 0x45))
