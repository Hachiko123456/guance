from __future__ import annotations

import pytest

from guance.infrastructure.model.marvel_v2 import (
    ExchangeTransferFacts, MarvelEvent, UnsupportedStateError, encode_event, encode_history,
)


def _payload(row):
    return row[:row.index(308) + 1]


def test_reset_is_public_to_all_four_observers():
    rows = encode_event(MarvelEvent("RESET", "LEAD_PLAY", 2, level=1))
    assert len(rows) == 4 and len(set(rows)) == 1
    assert _payload(rows[0]) == (1, 256, 2, 29, 40, 308)
    assert all(len(row) == 48 for row in rows)


def test_private_prefix_is_actor_only_and_other_rows_are_intentional_pad():
    rows = encode_event(MarvelEvent("PRIVATE_PREFIX", "FOLLOW_PLAY", 3, tokens=(11,)))
    assert _payload(rows[3]) == (1, 257, 3, 41, 11, 308)
    assert rows[:3] == ((0,) * 48,) * 3


def test_private_exchange_commit_is_actor_only():
    rows = encode_event(MarvelEvent("PRIVATE_EXCHANGE_COMMIT", "TRIBUTE", 1,
                                    tokens=(70, 7)))
    assert _payload(rows[1]) == (1, 259, 4, 39, 70, 7, 308)
    assert rows[0] == rows[2] == rows[3] == (0,) * 48


def test_public_commit_and_exchange_resolution_are_visible_to_all():
    commit = encode_event(MarvelEvent("PUBLIC_COMMIT", "FOLLOW_PLAY", 0,
                                      tokens=(6,)))
    assert _payload(commit[0]) == (1, 258, 3, 38, 6, 308)
    assert len(set(commit)) == 1
    transfer = ExchangeTransferFacts("tribute", 1, 0, 5)
    resolved = encode_event(MarvelEvent("EXCHANGE_RESOLVED", "TRIBUTE", 1,
                                        transfers=(transfer,), last_actor=1))
    assert _payload(resolved[0]) == (1, 260, 4, 39, 4, 39, 38, 69, 7, 308)
    assert len(set(resolved)) == 1


def test_history_shape_is_time_by_four_by_48_and_missing_is_unsupported():
    events = (
        MarvelEvent("RESET", "LEAD_PLAY", 0, level=3),
        MarvelEvent("PRIVATE_PREFIX", "LEAD_PLAY", 0, tokens=(8,)),
    )
    history = encode_history(events)
    assert len(history) == 2
    assert all(len(step) == 4 and all(len(row) == 48 for row in step) for step in history)
    with pytest.raises(UnsupportedStateError):
        encode_history(None)


def test_event_capacity_and_invalid_private_commit_fail_closed():
    with pytest.raises(ValueError):
        encode_event(MarvelEvent("PUBLIC_COMMIT", "LEAD_PLAY", 0,
                                 tokens=tuple(range(45))))
    with pytest.raises(ValueError):
        encode_event(MarvelEvent("PRIVATE_PREFIX", "LEAD_PLAY", 0,
                                 tokens=(8, 9)))


@pytest.mark.parametrize(
    "factory,code",
    (
        (lambda: MarvelEvent("PRIVATE_PREFIX", "LEAD_PLAY", 0, level=1, tokens=(8,)),
         "event_level_unexpected"),
        (lambda: MarvelEvent("PUBLIC_COMMIT", "FOLLOW_PLAY", 0, tokens=(6,),
                             transfers=(ExchangeTransferFacts("tribute", 1, 0, 5),)),
         "event_transfers_unexpected"),
        (lambda: MarvelEvent("PRIVATE_EXCHANGE_COMMIT", "TRIBUTE", 1,
                             tokens=(69, 7), last_actor=1),
         "event_last_actor_unexpected"),
        (lambda: MarvelEvent("EXCHANGE_RESOLVED", "TRIBUTE", 1, tokens=(8,),
                             transfers=(ExchangeTransferFacts("tribute", 1, 0, 5),),
                             last_actor=1),
         "event_tokens_unexpected"),
    ),
)
def test_event_kinds_reject_ignored_payload_fields(factory, code):
    with pytest.raises(ValueError, match=code):
        factory()


def test_event_payloads_must_be_deeply_immutable_tuples():
    with pytest.raises(ValueError, match="event_tokens_type_invalid"):
        encode_event(MarvelEvent("PRIVATE_PREFIX", "LEAD_PLAY", 0, tokens=[8]))
    with pytest.raises(ValueError, match="event_transfers_type_invalid"):
        encode_event(MarvelEvent(
            "EXCHANGE_RESOLVED", "TRIBUTE", 1,
            transfers=[ExchangeTransferFacts("tribute", 1, 0, 5)], last_actor=1,
        ))


def test_event_phase_and_actor_context_is_strict():
    with pytest.raises(ValueError, match="private_exchange_commit_invalid"):
        encode_event(MarvelEvent("PRIVATE_EXCHANGE_COMMIT", "LEAD_PLAY", 1,
                                 tokens=(69, 7)))
    with pytest.raises(ValueError, match="exchange_resolution_phase_invalid"):
        encode_event(MarvelEvent(
            "EXCHANGE_RESOLVED", "LEAD_PLAY", 1,
            transfers=(ExchangeTransferFacts("tribute", 1, 0, 5),), last_actor=1,
        ))
    with pytest.raises(ValueError, match="event_actor_last_actor_mismatch"):
        encode_event(MarvelEvent(
            "EXCHANGE_RESOLVED", "TRIBUTE", 0,
            transfers=(ExchangeTransferFacts("tribute", 1, 0, 5),), last_actor=1,
        ))
    with pytest.raises(ValueError, match="private_prefix_token_invalid"):
        encode_event(MarvelEvent("PRIVATE_PREFIX", "TRIBUTE", 1, tokens=(8,)))


def test_exchange_resolution_rejects_duplicate_transfer_identity_or_card():
    with pytest.raises(ValueError, match="event_transfers_nonunique"):
        encode_event(MarvelEvent(
            "EXCHANGE_RESOLVED", "TRIBUTE", 1,
            transfers=(
                ExchangeTransferFacts("tribute", 1, 0, 5),
                ExchangeTransferFacts("tribute", 1, 0, 6),
            ), last_actor=1,
        ))
    with pytest.raises(ValueError, match="event_transfers_nonunique"):
        encode_event(MarvelEvent(
            "EXCHANGE_RESOLVED", "RETURN", 0,
            transfers=(
                ExchangeTransferFacts("tribute", 1, 0, 5),
                ExchangeTransferFacts("return", 0, 1, 5),
            ), last_actor=0,
        ))



def test_event_dto_itself_rejects_phase_specific_private_and_public_tokens():
    with pytest.raises(ValueError, match="private_prefix_token_invalid"):
        MarvelEvent("PRIVATE_PREFIX", "TRIBUTE", 1, tokens=(8,))
    with pytest.raises(ValueError, match="event_tokens_invalid"):
        MarvelEvent("PUBLIC_COMMIT", "LEAD_PLAY", 0, tokens=(999, 7))
    with pytest.raises(ValueError, match="public_commit_sequence_invalid"):
        MarvelEvent("PUBLIC_COMMIT", "LEAD_PLAY", 0, tokens=(8, 18))
