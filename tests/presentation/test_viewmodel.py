"""Presentation-only tests; no network, proxy, or Windows certificate operations."""
from __future__ import annotations

from copy import deepcopy

import pytest
from PySide6.QtCore import QObject, QMetaObject, Qt
from PySide6.QtWidgets import QApplication

from guance.presentation.viewmodel import GuanceViewModel
from guance.presentation.formatting import public_play_display_order, seat_name


def test_advice_normalizes_numeric_pass_and_rank_claims(qapp: QApplication) -> None:
    vm = GuanceViewModel()
    vm.apply_observation({
        "session_id": "s", "game_id": "g", "state_version": 1,
        "level": 5, "my_seat": 0, "current_seat": 0, "hand": [0x15],
        "remaining": [1, 2, 3, 4],
    })
    vm.apply_advice({
        "session_id": "s", "game_id": "g", "state_version": 1,
        "status": "ready",
        "candidates": [
            {"kind": "0", "cards": [], "claim": [5, 11]},
        ],
    })

    candidate = vm.adviceCandidates[0]
    assert candidate["pass"] is True
    assert candidate["kind"] == "过牌"
    assert candidate["cards"] == []
    assert candidate["claim"] == ["5", "J"]


def test_debug_capture_toggle_emits_explicit_value(qapp: QApplication) -> None:
    vm = GuanceViewModel()
    values: list[bool] = []
    vm.debugCaptureRequested.connect(values.append)

    assert vm.debugCaptureEnabled is False
    vm.set_debug_capture_enabled(True)
    vm.set_debug_capture_enabled(False)

    assert values == [True, False]
    assert vm.debugCaptureEnabled is False

def test_authorize_and_remove_signals_carry_explicit_consent(qapp: QApplication) -> None:
    vm = GuanceViewModel()
    authorized: list[bool] = []
    removed: list[bool] = []
    vm.authorizeRequested.connect(authorized.append)
    vm.removeCertificateRequested.connect(removed.append)

    vm.request_authorize(False)
    vm.request_remove_certificate(False)
    assert authorized == []
    assert removed == []

    vm.request_authorize(True)
    vm.request_remove_certificate(True)
    assert authorized == [True]
    assert removed == [True]


def test_status_distinguishes_not_started_and_waiting_connection(qapp: QApplication) -> None:
    vm = GuanceViewModel()
    assert vm.statusTitle == "尚未启动"
    vm.apply_status({"phase": "waiting", "running": True, "connected": False})
    assert vm.statusTitle == "等待游戏连接"
    assert "等待" in vm.statusDetail


def invoke_click(object_: QObject) -> None:
    assert QMetaObject.invokeMethod(object_, "click", Qt.ConnectionType.DirectConnection)



def test_unknown_error_message_is_preserved(qapp: QApplication) -> None:
    vm = GuanceViewModel()
    vm.apply_status({"phase": "error", "error_message": "worker failed"})

    assert vm.lastError == "worker failed"
    assert vm.statusDetail == "worker failed"



def test_recent_plays_expose_only_public_latest_trick(qapp: QApplication) -> None:
    vm = GuanceViewModel()
    vm.apply_observation({
        "session_id": "s", "game_id": "g", "state_version": 2,
        "level": 5, "my_seat": 0, "current_seat": 1, "hand": [0x15],
        "remaining": [26, 27, 25, 27],
        "history": [
            {"index": 1, "player": 0, "kind": "play", "cards": [0x15], "trick": 2},
            {"index": 2, "player": 1, "kind": "pass", "cards": [], "trick": 2},
            {"index": 3, "player": 2, "kind": "play", "cards": [0x21], "trick": 2},
            {"index": 0, "player": 3, "kind": "play", "cards": [0x31], "trick": 1},
        ],
    })

    assert [item["name"] for item in vm.recentPlays] == ["自己", "右家", "对家", "左家"]
    assert [item["count"] for item in vm.recentPlays] == [26, 27, 25, 27]
    assert vm.recentPlays[0]["cardText"] == "♠5"
    assert vm.recentPlays[1]["cardText"] == "未出牌"
    assert vm.recentPlays[2]["cardText"] == "♥A"
    assert vm.recentPlays[3]["cardText"] == "过牌"
    assert vm.recentPlays[3]["active"] is True
    assert all(item["finishRank"] == -1 for item in vm.recentPlays)
    assert all(item["finishRankLabel"] == "" for item in vm.recentPlays)


def test_demo_recent_play_rows_have_explicit_unknown_finish_rank(qapp: QApplication) -> None:
    vm = GuanceViewModel()
    vm.enable_demo()

    assert vm.recentPlays
    assert all(item["finishRank"] == -1 for item in vm.recentPlays)
    assert all(item["finishRankLabel"] == "" for item in vm.recentPlays)


def _observation(*, game_id: str = "g", state_version: int = 1, hand=None, history=None) -> dict:
    return {
        "session_id": "s",
        "game_id": game_id,
        "state_version": state_version,
        "level": 5,
        "my_seat": 0,
        "current_seat": 0,
        "hand": list(hand or [0x15]),
        "remaining": [len(hand or [0x15]), 20, 20, 20],
        "history": list(history or []),
    }


def test_terminal_phase_clears_current_game_and_next_observation_restores_it(qapp: QApplication) -> None:
    vm = GuanceViewModel()
    first = _observation(game_id="first", state_version=1, history=[
        {"index": 1, "player": 0, "kind": "play", "cards": [0x15, 0x25], "trick": 1},
    ])
    vm.apply_observation(first)
    vm.apply_advice({
        "session_id": "s", "game_id": "first", "state_version": 1,
        "status": "ready", "candidates": [{"kind": "single", "cards": [0x15]}],
    })
    assert vm.observationAvailable is True
    assert vm.recentPlays
    assert vm.adviceVisible is True

    vm.apply_status({"phase": "ended"})
    assert vm.phase == "ended"
    assert vm.statusTitle == "本局已结束"
    assert vm.observationAvailable is False
    assert vm.handCards == []
    assert vm.recentPlays == []
    assert vm.adviceVisible is False

    vm.apply_status({"phase": "settled"})
    assert vm.phase == "settled"
    assert vm.observationAvailable is False

    # A late frame from the terminal game must not restore the old board.
    vm.apply_observation(_observation(game_id="first", state_version=2, hand=[0x31]))
    assert vm.observationAvailable is False

    second = _observation(game_id="second", state_version=1, hand=[0x31, 0x32])
    vm.apply_observation(second)
    assert vm.observationAvailable is True
    assert len(vm.handCards) == 2
    assert vm.phase == "ready"


def test_recent_plays_keep_all_five_eight_and_ten_cards(qapp: QApplication) -> None:
    vm = GuanceViewModel()
    for count in (5, 8, 10):
        cards = [0x15 + index for index in range(count)]
        vm.apply_observation(_observation(
            state_version=count,
            hand=cards[:1],
            history=[{"index": 1, "player": 0, "kind": "play", "cards": cards, "trick": 1}],
        ))
        assert len(vm.recentPlays[0]["cards"]) == count


def test_directory_requests_remain_available_without_copy_slots(qapp: QApplication) -> None:
    vm = GuanceViewModel()

    # Text selection is now handled by the read-only QML text controls; the
    # view model no longer exposes button-only clipboard slots.
    assert not hasattr(vm, "copy_last_error")
    assert not hasattr(vm, "copy_diagnostic")

    diagnostics: list[bool] = []
    captures: list[bool] = []
    vm.openDiagnosticsDirectoryRequested.connect(lambda: diagnostics.append(True))
    vm.openCaptureDirectoryRequested.connect(lambda: captures.append(True))
    vm.request_open_diagnostics_directory()
    vm.request_open_capture_directory()
    assert diagnostics == [True]
    assert captures == [True]


def test_table_state_is_independent_and_starts_with_absolute_seats(qapp: QApplication) -> None:
    vm = GuanceViewModel()
    vm.apply_status({"phase": "confirming_seat", "running": True})
    vm.apply_table_state({
        "session_id": "s", "game_id": "table-1", "state_version": 1,
        "phase": "confirming_seat", "level": 5, "my_seat": -1,
        "first_seat": 2, "current_seat": 2, "hand": [0x15, 0x25],
        "remaining": [27, 27, 27, 27],
        "history": [{"player": 2, "kind": "play", "cards": [0x11], "trick": 1}],
        "card_counter": [{"rank": rank, "total": 4, "played": 1, "in_hand": 0, "unseen": 3, "is_level": rank == 5}
                         for rank in list(range(1, 14)) + [14, 15]],
        "counter_unseen_total": 45,
    })

    assert vm.tableAvailable is True
    assert vm.seatConfirmed is False
    assert vm.tableHandCards and len(vm.tableHandCards) == 2
    assert [row["seat"] for row in vm.tableSeatRows] == [0, 1, 2, 3]
    assert all(row["name"].startswith("座位") for row in vm.tableSeatRows)
    assert len(vm.cardCounter) == 15
    assert vm.counterUnseenTotal == 45
    assert "待确认" in vm.seatOrderLabel

    vm.apply_table_state({
        "session_id": "s", "game_id": "table-1", "state_version": 2,
        "phase": "ready", "level": 5, "my_seat": 2, "first_seat": 2,
        "current_seat": 1, "seat_confirmed": True, "hand": [0x15],
        "remaining": [27, 26, 27, 27], "history": [],
        "card_counter": [], "counter_unseen_total": 0,
    })
    assert vm.seatConfirmed is True
    assert [row["seat"] for row in vm.tableSeatRows] == [2, 1, 0, 3]
    assert vm.seatOrderLabel == "逆时针：自己 → 右家 → 对家 → 左家"


def test_table_state_switch_clears_previous_game_display_and_advice(qapp: QApplication) -> None:
    vm = GuanceViewModel()
    vm.apply_table_state({"session_id": "s", "game_id": "one", "state_version": 1, "level": 5,
                          "my_seat": 0, "current_seat": 0, "seat_confirmed": True, "hand": [0x15],
                          "remaining": [1, 20, 20, 20], "history": []})
    vm.apply_observation(_observation(game_id="one", state_version=1))
    vm.apply_advice({"session_id": "s", "game_id": "one", "state_version": 1, "status": "ready",
                     "candidates": [{"kind": "single", "cards": [0x15]}]})
    assert vm.adviceVisible is True

    vm.apply_table_state({"session_id": "s", "game_id": "two", "state_version": 1, "level": 6,
                          "my_seat": -1, "seat_confirmed": False, "hand": [0x31],
                          "remaining": [27, 27, 27, 27], "history": []})
    assert vm.tableAvailable is True
    assert vm.tableLevel == 6
    assert vm.tableHandCards[0]["label"]
    assert vm.adviceVisible is False


def test_table_rows_include_finish_rank_labels(qapp: QApplication) -> None:
    vm = GuanceViewModel()
    vm.apply_table_state({
        "session_id": "s", "game_id": "ranked", "state_version": 1,
        "phase": "settled", "level": 5, "my_seat": 0,
        "current_seat": -1, "seat_confirmed": True,
        "finish_ranks_by_seat": [1, 2, 3, 4],
        "hand": [], "remaining": [0, 0, 0, 0], "history": [],
    })
    rows = {row["seat"]: row for row in vm.tableSeatRows}
    assert rows[0]["finishRankLabel"] == "头游"
    assert rows[1]["finishRankLabel"] == "二游"
    assert rows[2]["finishRankLabel"] == "三游"
    assert rows[3]["finishRankLabel"] == "末游"


def test_error_clears_live_table_but_terminal_keeps_public_snapshot(qapp: QApplication) -> None:
    vm = GuanceViewModel()
    vm.apply_table_state({"session_id": "s", "game_id": "g", "state_version": 1,
                          "phase": "active", "level": 5, "my_seat": 0,
                          "current_seat": 0, "seat_confirmed": True,
                          "hand": [0x15], "remaining": [1, 2, 3, 4],
                          "history": [], "card_counter": [],
                          "counter_public_remaining_total": 108})
    assert vm.boardAvailable is True

    vm.apply_status({"phase": "error", "error_message": "同步失败"})
    assert vm.boardAvailable is False
    assert vm.tableHandCards == []
    assert vm.tableSeatRows == []
    assert vm.cardCounter == []

    vm.apply_table_state({"session_id": "s", "game_id": "g", "state_version": 2,
                          "phase": "settled", "level": 5, "my_seat": 0,
                          "current_seat": -1, "seat_confirmed": True,
                          "hand": [], "remaining": [0, 2, 0, 3],
                          "history": [], "card_counter": [],
                          "counter_public_remaining_total": 5})
    vm.apply_status({"phase": "settled"})
    assert vm.boardAvailable is True
    assert vm.counterUnseenTotal == 5


def test_unconfirmed_table_still_names_current_absolute_seat_and_active_count(qapp: QApplication) -> None:
    vm = GuanceViewModel()
    vm.apply_status({"phase": "confirming_seat", "running": True})
    vm.apply_table_state({
        "session_id": "s", "game_id": "g", "state_version": 1,
        "phase": "active", "level": 5, "my_seat": -1,
        "first_seat": 3, "current_seat": 3, "seat_confirmed": False,
        "hand": [0x15], "remaining": [27, 26, 25, 24], "history": [],
        "card_counter": [], "counter_public_remaining_total": 108,
    })
    assert vm.turnLabel == "座位 3"
    active = next(row for row in vm.tableSeatRows if row["seat"] == 3)
    assert active["active"] is True
    assert active["count"] == 24

def test_set_floating_close_is_idempotent_for_real_viewmodel(qapp: QApplication) -> None:
    vm = GuanceViewModel()
    changes: list[bool] = []
    vm.floatingVisibleChanged.connect(lambda: changes.append(vm.floatingVisible))

    vm.set_floating(True)
    vm.set_floating(False)
    vm.set_floating(False)

    assert vm.floatingVisible is False
    assert changes == [True, False]

def test_manual_seat_confirmation_emits_only_allowed_unconfirmed_relations(qapp: QApplication) -> None:
    vm = GuanceViewModel()
    vm.apply_table_state({
        "session_id": "s", "game_id": "g", "state_version": 1,
        "phase": "confirming_seat", "my_seat": -1, "seat_confirmed": False,
        "current_seat": 2, "level": 5, "hand": [0x15],
        "remaining": [27, 27, 27, 27], "history": [],
    })
    requested: list[str] = []
    vm.confirmSeatRequested.connect(requested.append)

    vm.request_confirm_seat("right")
    vm.request_confirm_seat("numeric-2")
    vm.request_confirm_seat("self")

    assert requested == ["right", "self"]

    vm.apply_table_state({
        "session_id": "s", "game_id": "g", "state_version": 2,
        "phase": "active", "my_seat": 2, "seat_confirmed": True,
        "current_seat": 2, "level": 5, "hand": [0x15],
        "remaining": [27, 27, 27, 27], "history": [],
    })
    vm.request_confirm_seat("left")
    assert requested == ["right", "self"]


def test_display_hand_orders_jokers_level_and_high_cards(qapp: QApplication) -> None:
    vm = GuanceViewModel()
    vm.apply_table_state({"session_id": "s", "game_id": "sort", "state_version": 1,
                          "phase": "waiting_start", "level": 8, "my_seat": -1,
                          "first_seat": 0, "current_seat": -1, "seat_confirmed": False,
                          "hand": [0x12, 0x0E, 0x0F, 0x28, 0x2D, 0x11, 0x22],
                          "remaining": [27, 27, 27, 27], "history": [],
                          "card_counter": []})
    assert [card["label"] for card in vm.displayHandCards] == ["大王", "小王", "♥8", "♠A", "♥K", "♥2", "♠2"]


@pytest.mark.parametrize("source", ["observation", "table"])
def test_public_play_display_order_is_ascending_without_changing_hand_order(qapp: QApplication, source: str) -> None:
    vm = GuanceViewModel()
    cards = [0x0F, 0x128, 0x28, 0x2D, 0x11, 0x22, 0x112, 0x12, 0x0E]
    expected = [0x12, 0x112, 0x22, 0x2D, 0x11, 0x28, 0x128, 0x0E, 0x0F]
    data = {
        "session_id": "s", "game_id": "public-order", "state_version": 1,
        "phase": "ready", "level": 8, "my_seat": 0, "current_seat": 0, "seat_confirmed": True,
        "hand": cards, "remaining": [20, 20, 20, 20],
        "history": [{"player": 0, "kind": "play", "cards": cards, "trick": 1}],
    }
    original = deepcopy(data)
    vm.apply_observation(data)
    if source == "table":
        vm.apply_table_state(data)
    vm.apply_advice({
        "session_id": "s", "game_id": "public-order", "state_version": 1,
        "status": "ready", "candidates": [{"kind": "play", "cards": cards}],
    })

    assert public_play_display_order(cards, 8) == expected
    assert public_play_display_order(tuple(reversed(cards)), 8) == expected
    assert sorted(expected) == sorted(cards)
    assert [card["label"] for card in vm.displayHandCards] == ["大王", "小王", "♥8", "♥8", "♠A", "♥K", "♥2", "♠2", "♠2"]
    assert [card["label"] for card in vm.displaySeatRows[0]["cards"]] == ["♠2", "♠2", "♥2", "♥K", "♠A", "♥8", "♥8", "小王", "大王"]
    assert [card["label"] for card in vm.adviceCandidates[0]["cards"]] == ["大王", "♥8", "♥8", "♥K", "♠A", "♥2", "♠2", "♠2", "小王"]
    assert data == original
    assert vm._observation == original
    if source == "table":
        assert vm._table == original


@pytest.mark.parametrize("my_seat", range(4))
def test_relative_labels_keep_seat_semantics(my_seat: int) -> None:
    assert [seat_name((my_seat + offset) % 4, my_seat) for offset in (0, 3, 2, 1)] == ["自己", "右家", "对家", "左家"]


def test_saved_layout_preserves_size_mode_and_migrates_legacy(qapp: QApplication) -> None:
    vm = GuanceViewModel()
    assert vm.floatingLayout["sizeMode"] == "fit"
    legacy = {"saved": True, "mode": "free", "x": -900, "y": 30, "width": 520, "height": 430}
    vm.restore_floating_layout(legacy)
    assert vm.floatingLayout == legacy
    emitted = []
    vm.floatingLayoutSaveRequested.connect(emitted.append)
    vm.save_floating_layout({**legacy, "sizeMode": "fit"})
    assert emitted == [{**legacy, "sizeMode": "fit"}]


def test_model_metadata_projection_is_bounded_and_tracks_unavailable_state(qapp: QApplication) -> None:
    vm = GuanceViewModel()
    vm.apply_status({
        "model_state": "ready",
        "model_metadata": {
            "status": "loaded", "provider": "fabledan", "backend": "numpy",
            "runtime": "python", "model_version": "v7",
            "model_sha256": "abc123",
            "capabilities": {"recommend": True, "top_n": False},
            "private_detail": "must not cross presentation boundary",
        },
    })
    assert vm.modelMetadata == {
        "status": "loaded", "provider": "fabledan", "backend": "numpy",
        "runtime": "python", "model_version": "v7",
        "capabilities": {"recommend": True, "top_n": False},
        "fingerprint": "abc123",
    }

    vm.apply_status({"model_state": "unavailable", "model_metadata": {}})
    assert vm.modelState == "unavailable"
    assert vm.modelMetadata["status"] == "unavailable"
    assert vm.modelMetadata["provider"] == ""


def test_claim_display_requires_explicit_semantics_for_canonical_providers(qapp: QApplication) -> None:
    vm = GuanceViewModel()
    vm.apply_observation({
        "session_id": "s", "game_id": "claims", "state_version": 1,
        "level": 5, "my_seat": 0, "current_seat": 0, "hand": [0x15],
        "remaining": [1, 2, 3, 4],
    })
    base = {"session_id": "s", "game_id": "claims", "state_version": 1,
            "status": "ready"}

    vm.apply_advice({**base, "provider": "other", "model_version": "v1",
                     "candidates": [{"kind": "single", "cards": [0x15],
                                     "claim": [11, 14], "claim_kind": "opaque"}]})
    assert vm.adviceCandidates[0]["claim"] == ["11", "14"]

    vm.apply_advice({**base, "provider": "fabledan", "model_version": "v2",
                     "candidates": [{"kind": "single", "cards": [0x15],
                                     "claim": [11, 14],
                                     "claim_kind": "fabledan_rank_index"}]})
    assert vm.adviceCandidates[0]["claim"] == ["J", "小王"]


def test_phase6_experimental_metadata_is_honest_and_bounded(qapp: QApplication) -> None:
    vm = GuanceViewModel()
    vm.apply_status({
        "model_state": "unavailable",
        "model_metadata": {
            "status": "unavailable", "provider": "guandan_marvel_v2",
            "backend": "guandan", "runtime": "torch-cpu",
            "runtime_version": "2.13.0+cpu",
            "model_version": "MARVEL-V2-v1.3-iter000100",
            "model_sha256": "4" * 64, "bundle_fingerprint": "1" * 64,
            "deployment_fingerprint": "3" * 64,
            "score_kind": "marvel_v2_policy_path_score",
            "classification": "evaluation_only",
            "formal_evaluation_status": "not_run",
            "champion_status": "not_established",
            "experimental": True, "production_ready": False,
            "strength_claim": False, "public_self_supported": False,
            "capabilities": {"next_token_policy": True, "recommend": False},
            "native_path": r"D:\private\native.pyd",
            "source_root": r"D:\private\guandan",
        },
    })
    assert vm.modelState == "unavailable"
    assert vm.modelMetadata == {
        "status": "unavailable", "provider": "guandan_marvel_v2",
        "backend": "guandan", "runtime": "torch-cpu",
        "runtime_version": "2.13.0+cpu",
        "model_version": "MARVEL-V2-v1.3-iter000100",
        "capabilities": {"next_token_policy": True, "recommend": False},
        "fingerprint": "4" * 64, "bundle_fingerprint": "1" * 64,
        "deployment_fingerprint": "3" * 64,
        "score_kind": "marvel_v2_policy_path_score",
        "classification": "evaluation_only",
        "formal_evaluation_status": "not_run",
        "champion_status": "not_established",
        "experimental": True, "production_ready": False,
        "strength_claim": False, "public_self_supported": False,
    }
    assert "native_path" not in vm.modelMetadata and "source_root" not in vm.modelMetadata


def test_phase6_candidate_score_is_labelled_non_win_rate(qapp: QApplication) -> None:
    vm = GuanceViewModel()
    vm.apply_observation({
        "session_id": "s", "game_id": "score", "state_version": 1,
        "level": 5, "my_seat": 0, "current_seat": 0, "hand": [0x15],
        "remaining": [1, 2, 3, 4],
    })
    vm.apply_advice({
        "session_id": "s", "game_id": "score", "state_version": 1,
        "status": "ready", "provider": "guandan_marvel_v2",
        "model_version": "experimental", "score_kind": "marvel_v2_policy_path_score",
        "candidates": [{"kind": "single", "cards": [0x15], "claim": [],
                        "score": 0.75, "score_kind": "marvel_v2_policy_path_score"}],
    })
    candidate = vm.adviceCandidates[0]
    assert candidate["score"] == 0.75
    assert candidate["scoreKind"] == "marvel_v2_policy_path_score"
    assert candidate["scoreLabel"] == "策略/路径分数（非胜率）"
