"""Four-observer, 48-token MARVEL V2 event/history encoder."""
from __future__ import annotations

from typing import Iterable, Sequence

from .contracts import (
    BOS, CARD_TOKEN_BASE, COMMIT_TOKEN, EVENT_END, EVENT_EXCHANGE_RESOLVED,
    EVENT_PRIVATE_EXCHANGE_COMMIT, EVENT_PRIVATE_PREFIX, EVENT_PUBLIC_COMMIT,
    EVENT_RESET, EVENT_WIDTH, LENGTH_TOKEN, OBSERVER_COUNT, PHASE_TOKEN,
    TOKEN_FAMILY, TOKEN_LENGTH, TOKEN_RANK, TOKEN_SUIT, WILD_TOKEN_BASE,
    MarvelEvent, UnsupportedStateError, CardMapping, _reject,
)

_PAD_ROW = (0,) * EVENT_WIDTH


def _row(payload: Iterable[int]) -> tuple[int, ...]:
    body = tuple(payload)
    if any(type(token) is not int or not 0 <= token < 320 for token in body):
        _reject("event_token_invalid")
    framed = (BOS, *body, EVENT_END)
    if len(framed) > EVENT_WIDTH:
        _reject("event_row_overflow")
    return framed + (0,) * (EVENT_WIDTH - len(framed))


def encode_event(event: MarvelEvent) -> tuple[tuple[int, ...], ...]:
    event.validate()
    if event.kind == "RESET":
        assert event.level is not None
        row = _row((EVENT_RESET, PHASE_TOKEN[event.phase],
                    18 + CardMapping.level_to_rank(event.level) - 3,
                    LENGTH_TOKEN[event.actor]))
        return (row,) * OBSERVER_COUNT
    if event.kind == "PRIVATE_PREFIX":
        valid_private = (set(TOKEN_FAMILY) | set(TOKEN_RANK) | set(TOKEN_SUIT)
                         | set(TOKEN_LENGTH) | set(range(CARD_TOKEN_BASE, CARD_TOKEN_BASE + 108))
                         | set(range(WILD_TOKEN_BASE, WILD_TOKEN_BASE + 52)))
        if len(event.tokens) != 1 or event.tokens[0] not in valid_private:
            _reject("private_prefix_token_invalid")
        if (event.phase in {"TRIBUTE", "RETURN"}
                and not CARD_TOKEN_BASE <= event.tokens[0] < CARD_TOKEN_BASE + 108):
            _reject("private_prefix_token_invalid")
        row = _row((EVENT_PRIVATE_PREFIX, PHASE_TOKEN[event.phase],
                    LENGTH_TOKEN[event.actor], event.tokens[0]))
        return tuple(row if observer == event.actor else _PAD_ROW
                     for observer in range(OBSERVER_COUNT))
    if event.kind == "PRIVATE_EXCHANGE_COMMIT":
        if (event.phase not in {"TRIBUTE", "RETURN"} or len(event.tokens) != 2
                or not CARD_TOKEN_BASE <= event.tokens[0] < CARD_TOKEN_BASE + 108
                or event.tokens[1] != COMMIT_TOKEN):
            _reject("private_exchange_commit_invalid")
        row = _row((EVENT_PRIVATE_EXCHANGE_COMMIT, PHASE_TOKEN[event.phase],
                    LENGTH_TOKEN[event.actor], *event.tokens))
        return tuple(row if observer == event.actor else _PAD_ROW
                     for observer in range(OBSERVER_COUNT))
    if event.kind == "PUBLIC_COMMIT":
        if event.phase not in {"LEAD_PLAY", "FOLLOW_PLAY"}:
            _reject("public_commit_phase_invalid")
        if event.tokens != (6,) and (event.tokens[-1] != COMMIT_TOKEN or len(event.tokens) < 2):
            _reject("public_commit_sequence_invalid")
        if event.tokens == (6,) and event.phase != "FOLLOW_PLAY":
            _reject("public_pass_phase_invalid")
        row = _row((EVENT_PUBLIC_COMMIT, PHASE_TOKEN[event.phase],
                    LENGTH_TOKEN[event.actor], *event.tokens))
        return (row,) * OBSERVER_COUNT
    assert event.kind == "EXCHANGE_RESOLVED" and event.last_actor is not None
    records: list[int] = []
    for transfer in event.transfers:
        records.extend((PHASE_TOKEN["TRIBUTE" if transfer.kind == "tribute" else "RETURN"],
                        LENGTH_TOKEN[transfer.source_seat], LENGTH_TOKEN[transfer.target_seat],
                        CARD_TOKEN_BASE + transfer.card_id, COMMIT_TOKEN))
    row = _row((EVENT_EXCHANGE_RESOLVED, PHASE_TOKEN[event.phase],
                LENGTH_TOKEN[event.last_actor], *records))
    return (row,) * OBSERVER_COUNT


def encode_history(events: Sequence[MarvelEvent] | None) -> tuple[tuple[tuple[int, ...], ...], ...]:
    if events is None:
        raise UnsupportedStateError("exact_observer_history_required")
    result = tuple(encode_event(event) for event in events)
    if not result:
        raise UnsupportedStateError("exact_observer_history_required")
    if any(len(step) != OBSERVER_COUNT or any(len(row) != EVENT_WIDTH for row in step)
           for step in result):
        _reject("observer_history_shape_invalid")
    return result


__all__ = ["encode_event", "encode_history"]
