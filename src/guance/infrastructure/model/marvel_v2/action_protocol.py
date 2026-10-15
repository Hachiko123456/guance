"""Bounded immutable MARVEL V2 legal-row and prefix-session protocol."""
from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Iterable

from .action_decoder import ActionCandidate, decode_committed_tokens
from .contracts import (
    COMMIT_TOKEN, MAX_ACTION_ROWS, MAX_PREFIX_TOKENS, PASS_TOKEN, ActionProtocolError,
    ActionRow, CommitIdentity, DecodedAction, LegalActionRows, MarvelState,
    UnsupportedStateError,
    _int, _reject, _tuple_ints,
)


def generate_legal_rows(candidates: Iterable[ActionCandidate], *, phase: str,
                        prefix: Iterable[int] = (), session_id: str = "",
                        state_version: int = 0) -> LegalActionRows:
    """Generate every unique next-token row; never top-N truncate."""
    prefix_tuple = _tuple_ints(prefix, 0, 255, MAX_PREFIX_TOKENS,
                               code="prefix_invalid")
    candidate_tuple = tuple(candidates)
    if not candidate_tuple:
        raise ActionProtocolError("legal_candidates_missing")
    token_weights: dict[int, float] = {}
    complete_sequences: dict[tuple[int, ...], int] = {}
    for candidate in candidate_tuple:
        if type(candidate) is not ActionCandidate:
            _reject("candidate_type_invalid")
        candidate.validate()
        if candidate.phase != phase:
            raise ActionProtocolError("candidate_phase_mismatch")
        sequence = candidate.tokens
        complete_sequences[sequence] = complete_sequences.get(sequence, 0) + 1
        if len(prefix_tuple) >= len(sequence) or sequence[:len(prefix_tuple)] != prefix_tuple:
            continue
        token = sequence[len(prefix_tuple)]
        previous = token_weights.get(token)
        if previous is not None and previous != float(candidate.weight):
            raise ActionProtocolError("legal_row_weight_ambiguous")
        token_weights[token] = float(candidate.weight)
    if any(count != 1 for count in complete_sequences.values()):
        raise ActionProtocolError("legal_candidate_nonunique")
    if not token_weights:
        raise ActionProtocolError("prefix_has_no_legal_continuation")
    if len(token_weights) > MAX_ACTION_ROWS:
        raise ActionProtocolError("legal_rows_overflow")
    candidate_fingerprint = ";".join(sorted(
        f"{','.join(map(str, candidate.tokens))}@{float(candidate.weight):.17g}"
        for candidate in candidate_tuple
    ))
    generation = sha256(
        f"{session_id}|{state_version}|{phase}|{','.join(map(str, prefix_tuple))}|{candidate_fingerprint}".encode("utf-8")
    ).hexdigest()
    rows = tuple(ActionRow.for_token(phase, len(prefix_tuple), token,
                                     session_id=session_id, state_version=state_version,
                                     generation=generation)
                 for token in sorted(token_weights))
    weights = tuple(token_weights[row.token] for row in rows)
    result = LegalActionRows(rows, weights, tuple(row.is_commit for row in rows), phase,
                             prefix_tuple, session_id, state_version)
    result.validate()
    return result


@dataclass(frozen=True, slots=True)
class ProtocolStep:
    session: "ActionProtocolSession"
    committed_action: DecodedAction | None


@dataclass(frozen=True, slots=True)
class ActionProtocolSession:
    """One immutable private prefix session for a confirmed domain snapshot."""

    session_id: str
    state_version: int
    phase: str
    own_hand: tuple[int, ...]
    candidates: tuple[ActionCandidate, ...]
    confirmed_domain: object
    prefix: tuple[int, ...] = ()
    token_step: int = 0
    committed_step: int = 0
    closed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id or len(self.session_id) > 128:
            _reject("session_id_invalid")
        _int(self.state_version, 0, 2**31 - 1, "state_version_invalid")
        if self.phase not in {"LEAD_PLAY", "FOLLOW_PLAY", "TRIBUTE", "RETURN"}:
            _reject("session_phase_invalid")
        _tuple_ints(self.own_hand, 0, 107, 28, unique=True, code="own_hand_invalid")
        _tuple_ints(self.prefix, 0, 255, MAX_PREFIX_TOKENS, code="prefix_invalid")
        _int(self.token_step, 0, 2**31 - 1, "token_step_invalid")
        _int(self.committed_step, 0, 2**31 - 1, "committed_step_invalid")
        if self.committed_step > self.token_step or type(self.closed) is not bool:
            _reject("session_counter_invalid")
        if not self.candidates:
            raise ActionProtocolError("legal_candidates_missing")
        for candidate in self.candidates:
            candidate.validate()
            if candidate.phase != self.phase:
                raise ActionProtocolError("candidate_phase_mismatch")
        # Generation also proves uniqueness and current-prefix executability.
        if not self.closed:
            generate_legal_rows(self.candidates, phase=self.phase, prefix=self.prefix,
                                session_id=self.session_id, state_version=self.state_version)

    @classmethod
    def from_state(cls, state: MarvelState, *, session_id: str, state_version: int,
                   candidates: Iterable[ActionCandidate], confirmed_domain: object) -> "ActionProtocolSession":
        if type(state) is not MarvelState:
            raise UnsupportedStateError("marvel_state_required")
        state.validate()
        if state.phase == "TERMINAL" or not state.history_complete:
            raise UnsupportedStateError("action_protocol_state_unavailable")
        return cls(session_id, state_version, state.phase, state.own_hand, tuple(candidates),
                   confirmed_domain, state.private_prefix, state.token_step,
                   state.committed_step)

    @property
    def legal_rows(self) -> LegalActionRows:
        if self.closed:
            raise ActionProtocolError("session_closed")
        return generate_legal_rows(self.candidates, phase=self.phase, prefix=self.prefix,
                                   session_id=self.session_id, state_version=self.state_version)

    def clone(self) -> "ActionProtocolSession":
        """Return an equal immutable session; confirmed domain is not copied or mutated."""
        return replace(self)

    def select(self, row: ActionRow) -> ProtocolStep:
        if self.closed:
            raise ActionProtocolError("session_closed")
        if type(row) is not ActionRow:
            raise ActionProtocolError("row_type_invalid")
        if row.session_id != self.session_id or row.state_version != self.state_version:
            raise ActionProtocolError("row_foreign_or_stale")
        current = self.legal_rows
        exact = [candidate for candidate in current.rows if candidate == row]
        if len(exact) != 1 or current.weights[current.rows.index(row)] <= 0:
            raise ActionProtocolError("row_foreign_or_stale")
        token = row.token
        if token == PASS_TOKEN:
            if self.phase != "FOLLOW_PLAY" or self.prefix:
                raise ActionProtocolError("pass_not_legal")
            committed_tokens = (PASS_TOKEN,)
        elif token == COMMIT_TOKEN:
            if not self.prefix:
                raise ActionProtocolError("empty_commit_invalid")
            committed_tokens = self.prefix + (COMMIT_TOKEN,)
        else:
            if row.is_commit:
                raise ActionProtocolError("row_commit_metadata_invalid")
            if len(self.prefix) >= MAX_PREFIX_TOKENS:
                raise ActionProtocolError("prefix_overflow")
            next_session = replace(self, prefix=self.prefix + (token,),
                                   token_step=self.token_step + 1)
            return ProtocolStep(next_session, None)
        identity = CommitIdentity.from_tokens(
            self.session_id, self.state_version, self.token_step + 1, committed_tokens
        )
        action = decode_committed_tokens(committed_tokens, self.candidates,
                                         own_hand=self.own_hand, identity=identity)
        closed = replace(self, prefix=committed_tokens,
                         token_step=self.token_step + 1,
                         committed_step=self.committed_step + 1,
                         closed=True)
        return ProtocolStep(closed, action)


__all__ = ["ActionProtocolSession", "ProtocolStep", "generate_legal_rows"]
