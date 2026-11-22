"""Trusted-replay projection: verified provider state -> DeclaredFacts.v2.

Production counterpart of the offline acceptance bridge.  The DanKS
recommendation path reaches the model only when the
capture provider's confirmed public state for this turn has
been verified against the live observation, item by item:

- identity (session, game, state_version), observer/active seats, own hand
  and per-seat remaining counts;
- positive history proofs (public_history_complete / transport_contiguous
  known True) and contiguous verified action indices;
- tribute/return events confirmed as an opening prefix before the
  play/pass phase, which then aligns step by step;
- every history action's actor, kind and entity-card multiset verified
  BEFORE the declaration-canonical card order is normalized;
- only verified public declarations and the actual boolean evidence are
  projected into DeclaredFacts.v2 (history_origin ``live_capture``);
- the current winning fact matches the view's lead action.

An unconfirmed position yields no projection.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
from typing import Any


def project_trusted_replay_facts(observation: Any,
                                 provider: Any) -> tuple[Any, Any] | None:
    """Verified (normalized view, DeclaredFacts v2) for this turn, or None.

    ``observation`` is the live GameObservation behind the pending advice
    request; ``provider`` is the NormalExchangeLiveSubsetProvider whose
    decision queue holds this frame's confirmed public state.  Every
    mismatch raises inside :func:`_project_confirmed` and is reported as
    None - never a partially verified fact set.
    """
    try:
        decisions = provider.peek_decisions()
    except Exception:
        return None
    view = None
    try:
        from guance.contracts import CanonicalPublicGameView
        view = CanonicalPublicGameView.from_observation(observation)
    except Exception:
        return None
    public_states = [decision.public_state for decision in decisions]
    opening = getattr(provider, "opening_public_state", None)
    if opening is not None:
        public_states.append(opening)
    matches = []
    for public in public_states:
        identity = public.identity
        if (identity.session_id, identity.game_id,
                identity.state_version) == view.key:
            matches.append(public)
    if len(matches) != 1:
        return None  # no confirmed state, or an ambiguous duplicate: fail closed
    try:
        return _project_confirmed(matches[0], view)
    except Exception:
        return None


def _project_confirmed(public: Any, view: Any) -> tuple[Any, Any]:
    """Verify one confirmed public state against the observation view."""
    from guance.contracts import (DECLARED_FACTS_SCHEMA, ActionDeclarationFact,
                                  CurrentWinningDeclarationFact, DeclaredFacts)

    identity = public.identity
    if (identity.session_id, identity.game_id,
            identity.state_version) != view.key:
        raise ValueError("Public state identity mismatch")
    for fact, expected in ((public.observer_seat, view.my_seat),
                           (public.active_seat, view.current_seat)):
        if fact.status.value != "known" or fact.value != expected:
            raise ValueError("Seat mismatch")
    if (public.own_hand.status.value != "known"
            or Counter(public.own_hand.value) != Counter(view.hand)):
        raise ValueError("Own hand mismatch")
    if (public.remaining_counts.status.value != "known"
            or tuple(public.remaining_counts.value) != view.remaining):
        raise ValueError("Remaining counts mismatch")

    history = public.history
    for proof in (history.public_history_complete, history.transport_contiguous):
        if proof.status.value != "known" or proof.value is not True:
            raise ValueError("Positive history proof missing")
    if [action.index for action in history.actions] != list(
            range(1, len(history.actions) + 1)):
        raise ValueError("Noncontiguous verified history")
    plays = tuple(action for action in history.actions
                  if action.kind in ("play", "pass"))
    offset = len(history.actions) - len(plays)
    if not all(action.kind in ("tribute", "return")
               for action in history.actions[:offset]) \
            or tuple(history.actions[offset:]) != plays:
        raise ValueError("Unexpected exchange/play ordering")
    if len(plays) != len(view.public_history):
        raise ValueError("Play history lengths differ")

    normalized = []
    historical_facts = []
    for observed, verified in zip(view.public_history, plays):
        if (verified.index != offset + observed.index
                or verified.actor != observed.player
                or verified.kind != observed.kind
                or Counter(verified.cards) != Counter(observed.cards)):
            raise ValueError("History action mismatch")
        if verified.next_player.status.value == "known":
            if verified.next_player.value != observed.next_player:
                raise ValueError("Next actor mismatch")
        declaration = verified.declaration
        if observed.kind == "play":
            if declaration.status.value != "known":
                raise ValueError("Unconfirmed play declaration")
            declaration.value.validate_for_level(view.level)
            cards = tuple(declaration.value.canonical_cards)
        else:
            if declaration.status.value != "absent":
                raise ValueError("Unexpected PASS declaration")
            cards = ()
        if Counter(cards) != Counter(observed.cards):
            raise ValueError("Declared physical cards differ")
        normalized.append(replace(observed, cards=cards))
        historical_facts.append(_declaration_fact(ActionDeclarationFact,
                                                  declaration, observed.index))

    if view.lead_action is not None:
        if public.current_winning.status.value != "known":
            raise ValueError("Winning action unknown")
        knowledge = public.current_winning.value.declaration
        if knowledge.status.value != "known":
            raise ValueError("Winning declaration unknown")
        winning = _declaration_fact(CurrentWinningDeclarationFact, knowledge)
        lead = normalized[view.lead_action.index - 1]
        if (lead.index != view.lead_action.index
                or lead.player != view.lead_action.player
                or Counter(lead.cards) != Counter(view.lead_action.cards)):
            raise ValueError("Current target mismatch")
    else:
        if not view.free_lead or public.current_winning.status.value != "absent":
            raise ValueError("Unexpected free lead")
        winning = CurrentWinningDeclarationFact(
            status=public.current_winning.status.value)
        lead = None

    compatible = replace(view, public_history=tuple(normalized), lead_action=lead)
    facts = DeclaredFacts(
        DECLARED_FACTS_SCHEMA, winning,
        history_declarations=tuple(historical_facts),
        history_origin="live_capture",
        public_history_complete=history.public_history_complete.status.value,
        history_complete_value=history.public_history_complete.value,
        transport_contiguous=history.transport_contiguous.status.value,
        transport_value=history.transport_contiguous.value)
    facts.validate_for_view(compatible)
    return compatible, facts


def _declaration_fact(fact_type, knowledge, index: int | None = None):
    fields: dict[str, Any] = {}
    if index is not None:
        fields["index"] = index
    fields["status"] = knowledge.status.value
    if knowledge.status.value == "known":
        fields["declaration"] = knowledge.value
    elif knowledge.status.value != "absent":
        fields["reason"] = knowledge.reason or ""
    return fact_type(**fields)


__all__ = ["project_trusted_replay_facts"]
