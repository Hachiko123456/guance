from __future__ import annotations

from guance.domain.cards import card_label, encode_card, full_deck, validate_deck
from guance.domain.events import ActionEvent, DealEvent, SourceMeta, StartEvent
from guance.domain.state import GameState, StateError


def test_card_codec_covers_two_deck_physical_cards() -> None:
    deck = full_deck()
    validate_deck(deck)
    assert len(deck) == len(set(deck)) == 108
    assert card_label(encode_card(2, 2, 0)) == "H2"
    assert card_label(encode_card(0, 14, 0)) == "JOKER_SMALL"


def test_state_advances_only_on_confirmed_actions() -> None:
    state = GameState("fixture")
    hand = tuple(full_deck()[:27])
    state.apply_deal(DealEvent(my_seat=2, level=5, hand=hand))
    state.apply_start(StartEvent(first_seat=2))
    state.apply_action(ActionEvent(player=2, kind="play", cards=(hand[0],),
                                   combo_type=1, next_player=1, remaining_after=26,
                                   action_index=1, expected_trick=0))
    assert state.snapshot().state_version == 3
    try:
        state.apply_action(ActionEvent(player=3, kind="pass", next_player=0,
                                       action_index=2, expected_trick=0))
    except StateError:
        pass
    else:
        raise AssertionError("actor mismatch must fail closed")
    assert state.snapshot().state_version == 3
