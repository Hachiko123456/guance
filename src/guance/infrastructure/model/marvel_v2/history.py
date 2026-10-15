"""Exact four-observer MARVEL V2 history and immutable branch workspace."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from hashlib import sha256
from typing import Sequence

from .contracts import (
    BOS, CARD_TOKEN_BASE, COMMIT_TOKEN, EVENT_END, EVENT_EXCHANGE_RESOLVED,
    EVENT_PRIVATE_EXCHANGE_COMMIT, EVENT_PRIVATE_PREFIX, EVENT_PUBLIC_COMMIT,
    EVENT_RESET, EVENT_WIDTH, LENGTH_TOKEN, MAX_PREFIX_TOKENS, OBSERVER_COUNT,
    PASS_TOKEN, PHASE_TOKEN, RANK_TOKEN, TOKEN_FAMILY, TOKEN_LENGTH, TOKEN_PHASE, TOKEN_RANK,
    TOKEN_SUIT, WILD_TOKEN_BASE, MarvelEvent, UnsupportedStateError, action_token_kind,
)
from .event_encoder import encode_event
from .lifecycle_contracts import (
    MAX_BRANCHES, MAX_HISTORY_STEPS, HistoryCursor, HistoryIdentity,
    bounded_count, bounded_id, fail, sha256_hex,
)

ObserverRow = tuple[int, ...]
ObserverStepRows = tuple[ObserverRow, ObserverRow, ObserverRow, ObserverRow]
_PAD_ROW: ObserverRow = (0,) * EVENT_WIDTH
_PUBLIC_EVENTS = frozenset((EVENT_RESET, EVENT_PUBLIC_COMMIT, EVENT_EXCHANGE_RESOLVED))
_PRIVATE_EVENTS = frozenset((EVENT_PRIVATE_PREFIX, EVENT_PRIVATE_EXCHANGE_COMMIT))
_EVENT_NAMES = {
    EVENT_RESET: "RESET", EVENT_PRIVATE_PREFIX: "PRIVATE_PREFIX",
    EVENT_PUBLIC_COMMIT: "PUBLIC_COMMIT",
    EVENT_PRIVATE_EXCHANGE_COMMIT: "PRIVATE_EXCHANGE_COMMIT",
    EVENT_EXCHANGE_RESOLVED: "EXCHANGE_RESOLVED",
}


def _payload(row: ObserverRow) -> tuple[int, ...]:
    if type(row) is not tuple:
        fail("observer_history_row_type_invalid")
    if len(row) != EVENT_WIDTH or any(type(token) is not int or not 0 <= token < 320 for token in row):
        fail("observer_history_row_invalid")
    if row == _PAD_ROW:
        return ()
    if row[0] != BOS or EVENT_END not in row:
        fail("observer_history_frame_invalid")
    end = row.index(EVENT_END)
    if end < 2 or any(row[end + 1:]):
        fail("observer_history_frame_invalid")
    return row[:end + 1]


@dataclass(frozen=True, slots=True)
class HistoryStep:
    rows: ObserverStepRows
    kind: str = field(init=False)
    phase: str = field(init=False)
    actor: int = field(init=False)

    def __post_init__(self) -> None:
        if type(self.rows) is not tuple or len(self.rows) != OBSERVER_COUNT:
            fail("exact_four_observers_required")
        payloads = tuple(_payload(row) for row in self.rows)
        visible = tuple(index for index, payload in enumerate(payloads) if payload)
        if not visible:
            fail("observer_history_step_missing")
        first = payloads[visible[0]]
        event_code = first[1]
        if event_code not in _EVENT_NAMES:
            fail("observer_history_event_invalid")
        if event_code in _PUBLIC_EVENTS:
            if visible != (0, 1, 2, 3) or len(set(payloads)) != 1:
                fail("public_event_visibility_invalid")
        else:
            if event_code not in _PRIVATE_EVENTS or len(visible) != 1:
                fail("private_event_visibility_invalid")
        try:
            phase = TOKEN_PHASE[first[2]]
        except (IndexError, KeyError):
            fail("observer_history_phase_invalid")
        actor_position = 4 if event_code == EVENT_RESET else 3
        try:
            actor_token = first[actor_position]
            actor = next(seat for seat, token in LENGTH_TOKEN.items() if token == actor_token)
        except (IndexError, StopIteration):
            fail("observer_history_actor_invalid")
        if event_code in _PRIVATE_EVENTS and visible != (actor,):
            fail("private_event_actor_visibility_invalid")
        expected_lengths = {EVENT_RESET: 6, EVENT_PRIVATE_PREFIX: 6,
                            EVENT_PRIVATE_EXCHANGE_COMMIT: 7}
        if event_code in expected_lengths and len(first) != expected_lengths[event_code]:
            fail("observer_history_event_shape_invalid")
        if event_code == EVENT_PUBLIC_COMMIT and len(first) < 5:
            fail("observer_history_event_shape_invalid")
        if (event_code == EVENT_EXCHANGE_RESOLVED
                and (len(first) <= 5 or (len(first) - 5) % 5)):
            fail("observer_history_event_shape_invalid")
        if (event_code == EVENT_RESET
                and first[3] not in {RANK_TOKEN[rank] for rank in range(3, 16)}):
            fail("history_reset_level_invalid")
        if event_code == EVENT_PRIVATE_PREFIX:
            private_tokens = (set(TOKEN_FAMILY) | set(TOKEN_RANK) | set(TOKEN_SUIT)
                              | set(TOKEN_LENGTH)
                              | set(range(CARD_TOKEN_BASE, CARD_TOKEN_BASE + 108))
                              | set(range(WILD_TOKEN_BASE, WILD_TOKEN_BASE + 52)))
            if first[4] not in private_tokens:
                fail("history_private_prefix_invalid")
            if (phase in {"TRIBUTE", "RETURN"}
                    and not CARD_TOKEN_BASE <= first[4] < CARD_TOKEN_BASE + 108):
                fail("history_private_prefix_invalid")
        if event_code == EVENT_PRIVATE_EXCHANGE_COMMIT:
            if (phase not in {"TRIBUTE", "RETURN"}
                    or not CARD_TOKEN_BASE <= first[4] < CARD_TOKEN_BASE + 108
                    or first[5] != COMMIT_TOKEN):
                fail("history_exchange_commit_invalid")
        if event_code == EVENT_PUBLIC_COMMIT:
            tokens = first[4:-1]
            if phase not in {"LEAD_PLAY", "FOLLOW_PLAY"}:
                fail("history_public_commit_phase_invalid")
            if tokens == (PASS_TOKEN,):
                if phase != "FOLLOW_PLAY":
                    fail("history_pass_context_invalid")
            elif not tokens or tokens[-1] != COMMIT_TOKEN:
                fail("history_public_commit_invalid")
            for token in tokens:
                try:
                    action_token_kind(token)
                except ValueError:
                    fail("history_public_commit_invalid")
        if event_code == EVENT_EXCHANGE_RESOLVED:
            if phase not in {"TRIBUTE", "RETURN"}:
                fail("history_exchange_resolution_phase_invalid")
            records = first[4:-1]
            cards: set[int] = set()
            transfers: set[tuple[int, int, int]] = set()
            for offset in range(0, len(records), 5):
                record = records[offset:offset + 5]
                if (record[0] not in {PHASE_TOKEN["TRIBUTE"], PHASE_TOKEN["RETURN"]}
                        or record[1] not in LENGTH_TOKEN.values()
                        or record[2] not in LENGTH_TOKEN.values()
                        or record[1] == record[2]
                        or not CARD_TOKEN_BASE <= record[3] < CARD_TOKEN_BASE + 108
                        or record[4] != COMMIT_TOKEN
                        or record[3] in cards
                        or (record[0], record[1], record[2]) in transfers):
                    fail("history_exchange_resolution_invalid")
                cards.add(record[3])
                transfers.add((record[0], record[1], record[2]))
        object.__setattr__(self, "kind", _EVENT_NAMES[event_code])
        object.__setattr__(self, "phase", phase)
        object.__setattr__(self, "actor", actor)

    @property
    def event_code(self) -> int:
        for row in self.rows:
            if row != _PAD_ROW:
                return row[1]
        raise AssertionError("validated history step has no visible row")

    @property
    def visible_row(self) -> ObserverRow:
        return next(row for row in self.rows if row != _PAD_ROW)

    @property
    def event_tokens(self) -> tuple[int, ...]:
        payload = _payload(self.visible_row)
        if self.event_code == EVENT_RESET:
            return ()
        return payload[4:-1]

    @property
    def exchange_records(self) -> tuple[tuple[str, int, int, int], ...]:
        if self.event_code != EVENT_EXCHANGE_RESOLVED:
            return ()
        payload = _payload(self.visible_row)
        records = payload[4:-1]
        decoded = []
        for offset in range(0, len(records), 5):
            phase_token, source_token, target_token, card_token, _commit = records[offset:offset + 5]
            phase = TOKEN_PHASE[phase_token]
            source = next(seat for seat, token in LENGTH_TOKEN.items() if token == source_token)
            target = next(seat for seat, token in LENGTH_TOKEN.items() if token == target_token)
            decoded.append((phase, source, target, card_token))
        return tuple(decoded)


def _advance(cursor: HistoryCursor | None, prefix: tuple[int, ...],
             pending_exchange: tuple[tuple[str, int, int], ...],
             step: HistoryStep, index: int
             ) -> tuple[HistoryCursor, tuple[int, ...], tuple[tuple[str, int, int], ...]]:
    code = step.event_code
    play_phases = {"LEAD_PLAY", "FOLLOW_PLAY"}
    exchange_phases = {"TRIBUTE", "RETURN"}
    if code == EVENT_RESET:
        if index != 0 or cursor is not None or prefix or pending_exchange:
            fail("history_reset_position_invalid")
        result = HistoryCursor(step.phase, step.actor, 0, 0, 0, 0, True, False)
        result.validate(); return result, (), ()
    if cursor is None:
        fail("history_reset_required")
    token_step, committed_step = cursor.token_step, cursor.committed_step
    if cursor.awaiting_play and step.phase != "LEAD_PLAY":
        fail("history_phase_transition_invalid")
    if cursor.phase == "RETURN" and step.phase == "TRIBUTE":
        fail("history_phase_transition_invalid")
    if (not cursor.awaiting_play and cursor.phase in exchange_phases
            and cursor.context_bound is False and step.phase not in exchange_phases):
        fail("history_phase_transition_invalid")
    if (not cursor.awaiting_play and cursor.phase in play_phases
            and cursor.context_bound is False and step.phase not in play_phases):
        fail("history_phase_transition_invalid")
    if code == EVENT_PRIVATE_PREFIX:
        if cursor.context_bound and (cursor.actor != step.actor or cursor.phase != step.phase):
            fail("history_action_context_mismatch")
        if len(prefix) >= MAX_PREFIX_TOKENS:
            raise UnsupportedStateError("history_prefix_overflow")
        tokens = step.event_tokens
        if len(tokens) != 1:
            fail("history_private_prefix_invalid")
        prefix = prefix + tokens
        token_step += 1
        result = HistoryCursor(step.phase, step.actor, token_step, committed_step,
                               len(prefix), len(pending_exchange), True, False)
        result.validate(); return result, prefix, pending_exchange
    if code == EVENT_PUBLIC_COMMIT:
        if cursor.context_bound and (cursor.actor != step.actor or cursor.phase != step.phase):
            fail("history_action_context_mismatch")
        tokens = step.event_tokens
        if tokens == (PASS_TOKEN,):
            if prefix or step.phase != "FOLLOW_PLAY":
                fail("history_pass_context_invalid")
        elif (not prefix or tokens != prefix + (COMMIT_TOKEN,)):
            fail("history_commit_prefix_mismatch")
        token_step += 1; committed_step += 1
        result = HistoryCursor(step.phase, step.actor, token_step, committed_step,
                               0, len(pending_exchange), False, False)
        result.validate(); return result, (), pending_exchange
    if code == EVENT_PRIVATE_EXCHANGE_COMMIT:
        if not cursor.context_bound or cursor.actor != step.actor or cursor.phase != step.phase:
            fail("history_action_context_mismatch")
        tokens = step.event_tokens
        if (len(prefix) != 1 or len(tokens) != 2
                or tokens != prefix + (COMMIT_TOKEN,)):
            fail("history_exchange_commit_prefix_mismatch")
        identity = (step.phase, step.actor, tokens[0])
        if (any(item[:2] == identity[:2] for item in pending_exchange)
                or any(item[2] == identity[2] for item in pending_exchange)):
            fail("history_exchange_commit_duplicate")
        pending_exchange = pending_exchange + (identity,)
        token_step += 1; committed_step += 1
        result = HistoryCursor(step.phase, step.actor, token_step, committed_step,
                               0, len(pending_exchange), False, False)
        result.validate(); return result, (), pending_exchange
    assert code == EVENT_EXCHANGE_RESOLVED
    if not cursor.context_bound or cursor.actor != step.actor or cursor.phase != step.phase:
        fail("history_action_context_mismatch")
    if len(prefix) != 1 or not pending_exchange:
        fail("history_exchange_resolution_without_prefix")
    final_identity = (step.phase, step.actor, prefix[0])
    if (any(item[:2] == final_identity[:2] for item in pending_exchange)
            or any(item[2] == final_identity[2] for item in pending_exchange)):
        fail("history_exchange_commit_duplicate")
    expected = pending_exchange + (final_identity,)
    actual = tuple((phase, source, card)
                   for phase, source, _target, card in step.exchange_records)
    if actual != expected:
        fail("history_exchange_resolution_mismatch")
    token_step += 1; committed_step += 1
    result = HistoryCursor(step.phase, step.actor, token_step, committed_step,
                           0, 0, False, True)
    result.validate(); return result, (), ()


def _history_digest(steps: Sequence[HistoryStep]) -> str:
    h = sha256(b"guance-marvel-v2-four-observer-history-v1")
    h.update(len(steps).to_bytes(4, "big"))
    for step in steps:
        for row in step.rows:
            for token in row:
                h.update(token.to_bytes(2, "big"))
    return h.hexdigest()


@dataclass(frozen=True, slots=True)
class ExactHistoryJournal:
    session_id: str
    game_id: str
    state_id: str
    artifact_id: str
    artifact_sha256: str
    steps: tuple[HistoryStep, ...] = ()
    max_steps: int = MAX_HISTORY_STEPS
    branch_id: str = "confirmed"
    _cursor: HistoryCursor | None = field(init=False, repr=False)
    _history_sha256: str | None = field(init=False, repr=False)

    def __post_init__(self) -> None:
        bounded_id(self.session_id, "history_session_id_invalid")
        bounded_id(self.game_id, "history_game_id_invalid")
        bounded_id(self.state_id, "history_state_id_invalid")
        bounded_id(self.artifact_id, "history_artifact_id_invalid")
        bounded_id(self.branch_id, "history_branch_id_invalid")
        sha256_hex(self.artifact_sha256, "history_artifact_hash_invalid")
        bounded_count(self.max_steps, 1, MAX_HISTORY_STEPS, "history_horizon_invalid")
        if type(self.steps) is not tuple:
            fail("history_steps_type_invalid")
        if len(self.steps) > self.max_steps:
            raise UnsupportedStateError("history_horizon_exceeded")
        cursor = None
        prefix: tuple[int, ...] = ()
        pending_exchange: tuple[tuple[str, int, int], ...] = ()
        for index, step in enumerate(self.steps):
            if type(step) is not HistoryStep:
                fail("history_step_invalid")
            cursor, prefix, pending_exchange = _advance(
                cursor, prefix, pending_exchange, step, index
            )
        object.__setattr__(self, "_cursor", cursor)
        object.__setattr__(self, "_history_sha256", _history_digest(self.steps) if self.steps else None)

    @classmethod
    def from_events(cls, *, session_id: str, game_id: str, state_id: str,
                    artifact_id: str, artifact_sha256: str,
                    events: Sequence[MarvelEvent] | None,
                    max_steps: int = MAX_HISTORY_STEPS,
                    branch_id: str = "confirmed") -> "ExactHistoryJournal":
        if events is None or not events:
            raise UnsupportedStateError("exact_observer_history_required")
        journal = cls(session_id, game_id, state_id, artifact_id, artifact_sha256,
                      max_steps=max_steps, branch_id=branch_id)
        for event in events:
            journal = journal.append(event)
        return journal

    @classmethod
    def from_observer_streams(cls, *, session_id: str, game_id: str, state_id: str,
                              artifact_id: str, artifact_sha256: str,
                              observer_streams: Sequence[Sequence[Sequence[int]]] | None,
                              max_steps: int = MAX_HISTORY_STEPS,
                              branch_id: str = "confirmed") -> "ExactHistoryJournal":
        if observer_streams is None or len(observer_streams) != OBSERVER_COUNT:
            raise UnsupportedStateError("exact_four_observers_required")
        lengths = tuple(len(stream) for stream in observer_streams)
        if not lengths or not lengths[0] or len(set(lengths)) != 1:
            raise UnsupportedStateError("exact_observer_history_required")
        if lengths[0] > max_steps:
            raise UnsupportedStateError("history_horizon_exceeded")
        steps = tuple(
            HistoryStep(tuple(tuple(observer_streams[observer][index])
                              for observer in range(OBSERVER_COUNT)))
            for index in range(lengths[0])
        )
        return cls(session_id, game_id, state_id, artifact_id, artifact_sha256,
                   steps, max_steps, branch_id)

    @property
    def exact(self) -> bool:
        return bool(self.steps)

    @property
    def cursor(self) -> HistoryCursor:
        if self._cursor is None:
            raise UnsupportedStateError("exact_observer_history_required")
        return self._cursor

    @property
    def history_sha256(self) -> str:
        if self._history_sha256 is None:
            raise UnsupportedStateError("exact_observer_history_required")
        return self._history_sha256

    @property
    def observer_streams(self) -> tuple[tuple[ObserverRow, ...], ...]:
        if not self.steps:
            raise UnsupportedStateError("exact_observer_history_required")
        return tuple(tuple(step.rows[observer] for step in self.steps)
                     for observer in range(OBSERVER_COUNT))

    @property
    def identity(self) -> HistoryIdentity:
        result = HistoryIdentity(
            self.session_id, self.game_id, self.state_id, self.artifact_id,
            self.artifact_sha256, self.history_sha256, self.branch_id,
            len(self.steps), self.cursor, True,
        )
        result.validate(); return result

    def append(self, event: MarvelEvent, *, state_id: str | None = None) -> "ExactHistoryJournal":
        if len(self.steps) >= self.max_steps:
            raise UnsupportedStateError("history_horizon_exceeded")
        if type(event) is not MarvelEvent:
            fail("history_event_invalid")
        step = HistoryStep(tuple(tuple(row) for row in encode_event(event)))
        next_state = self.state_id if state_id is None else bounded_id(
            state_id, "history_state_id_invalid"
        )
        return replace(self, state_id=next_state, steps=self.steps + (step,))

    def clone_branch(self, branch_id: str, *, state_id: str | None = None) -> "SpeculativeHistoryBranch":
        bounded_id(branch_id, "history_branch_id_invalid")
        if branch_id == "confirmed":
            fail("speculative_branch_id_invalid")
        return SpeculativeHistoryBranch(
            confirmed=self, branch_id=branch_id,
            state_id=self.state_id if state_id is None else state_id,
        )


@dataclass(frozen=True, slots=True)
class SpeculativeHistoryBranch:
    confirmed: ExactHistoryJournal
    branch_id: str
    state_id: str
    extra_steps: tuple[HistoryStep, ...] = ()

    def __post_init__(self) -> None:
        if type(self.confirmed) is not ExactHistoryJournal or not self.confirmed.exact:
            raise UnsupportedStateError("exact_observer_history_required")
        if type(self.extra_steps) is not tuple:
            fail("branch_steps_type_invalid")
        bounded_id(self.branch_id, "history_branch_id_invalid")
        bounded_id(self.state_id, "history_state_id_invalid")
        if self.branch_id == "confirmed":
            fail("speculative_branch_id_invalid")
        if len(self.confirmed.steps) + len(self.extra_steps) > self.confirmed.max_steps:
            raise UnsupportedStateError("history_horizon_exceeded")
        self.snapshot

    @property
    def snapshot(self) -> ExactHistoryJournal:
        return ExactHistoryJournal(
            self.confirmed.session_id, self.confirmed.game_id, self.state_id,
            self.confirmed.artifact_id, self.confirmed.artifact_sha256,
            self.confirmed.steps + self.extra_steps, self.confirmed.max_steps,
            self.branch_id,
        )

    @property
    def identity(self) -> HistoryIdentity:
        return self.snapshot.identity

    def append(self, event: MarvelEvent, *, state_id: str | None = None) -> "SpeculativeHistoryBranch":
        current = self.snapshot
        updated = current.append(event, state_id=self.state_id if state_id is None else state_id)
        return replace(self, state_id=updated.state_id,
                       extra_steps=updated.steps[len(self.confirmed.steps):])

    def clone(self, branch_id: str) -> "SpeculativeHistoryBranch":
        bounded_id(branch_id, "history_branch_id_invalid")
        if branch_id in {"confirmed", self.branch_id}:
            fail("speculative_branch_id_invalid")
        return replace(self, branch_id=branch_id)


@dataclass(frozen=True, slots=True)
class HistoryWorkspace:
    confirmed: ExactHistoryJournal
    branches: tuple[SpeculativeHistoryBranch, ...] = ()
    max_branches: int = MAX_BRANCHES

    def __post_init__(self) -> None:
        if type(self.confirmed) is not ExactHistoryJournal or self.confirmed.branch_id != "confirmed":
            fail("confirmed_history_invalid")
        if type(self.branches) is not tuple:
            fail("branch_collection_type_invalid")
        bounded_count(self.max_branches, 1, MAX_BRANCHES, "branch_limit_invalid")
        if len(self.branches) > self.max_branches:
            raise UnsupportedStateError("branch_limit_exceeded")
        ids = []
        for branch in self.branches:
            if type(branch) is not SpeculativeHistoryBranch:
                fail("speculative_branch_invalid")
            if branch.confirmed != self.confirmed:
                fail("speculative_branch_base_mismatch")
            ids.append(branch.branch_id)
        if len(ids) != len(set(ids)):
            fail("speculative_branch_duplicate")

    def branch(self, branch_id: str) -> SpeculativeHistoryBranch:
        matches = tuple(branch for branch in self.branches if branch.branch_id == branch_id)
        if len(matches) != 1:
            fail("speculative_branch_missing")
        return matches[0]

    def clone_branch(self, branch_id: str, *, source_branch_id: str | None = None,
                     state_id: str | None = None) -> "HistoryWorkspace":
        if len(self.branches) >= self.max_branches:
            raise UnsupportedStateError("branch_limit_exceeded")
        if any(branch.branch_id == branch_id for branch in self.branches):
            fail("speculative_branch_duplicate")
        if source_branch_id is None:
            new = self.confirmed.clone_branch(branch_id, state_id=state_id)
        else:
            source = self.branch(source_branch_id)
            new = source.clone(branch_id)
            if state_id is not None:
                new = replace(new, state_id=state_id)
        return replace(self, branches=self.branches + (new,))

    def append_speculative(self, branch_id: str, event: MarvelEvent, *,
                           state_id: str | None = None) -> "HistoryWorkspace":
        current = self.branch(branch_id)
        updated = current.append(event, state_id=state_id)
        return replace(self, branches=tuple(
            updated if branch.branch_id == branch_id else branch for branch in self.branches
        ))

    def append_confirmed(self, event: MarvelEvent, *, state_id: str | None = None) -> "HistoryWorkspace":
        """Only an observed confirmed event advances confirmed history; branches expire."""
        return HistoryWorkspace(self.confirmed.append(event, state_id=state_id), (), self.max_branches)


ExactObserverHistory = ExactHistoryJournal
SpeculativeBranch = SpeculativeHistoryBranch

__all__ = [
    "ExactHistoryJournal", "ExactObserverHistory", "HistoryStep", "HistoryWorkspace",
    "ObserverRow", "ObserverStepRows", "SpeculativeHistoryBranch", "SpeculativeBranch",
]
