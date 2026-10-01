"""Offscreen QML smoke tests for user-facing presentation interactions."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, QMetaObject, Qt
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from guance.presentation.app import create_ui
from guance.presentation.viewmodel import GuanceViewModel
from guance.domain.cards import full_deck

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def ui(qapp: QApplication):
    vm = GuanceViewModel()
    engine = create_ui(qapp, vm)
    qapp.processEvents()
    root = engine.rootObjects()[0]
    yield qapp, engine, root, vm
    root.hide()
    qapp.processEvents()


def _child(root: QObject, name: str) -> QObject:
    child = root.findChild(QObject, name)
    assert child is not None, f"QML object {name!r} was not created"
    return child


def _maybe_child(root: QObject, name: str) -> QObject | None:
    return root.findChild(QObject, name)


def _click(obj: QObject) -> None:
    assert QMetaObject.invokeMethod(obj, "click", Qt.ConnectionType.DirectConnection)


def _settle(app: QApplication, milliseconds: int = 220) -> None:
    deadline = time.monotonic() + milliseconds / 1000
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()


def test_floating_trigger_opens_closes_and_reopens(ui) -> None:
    app, _engine, root, vm = ui
    floating = _child(root, "floatingAdviceWindow")
    trigger = _child(root, "floatingButton")

    assert floating.property("visible") is False
    _click(trigger)
    app.processEvents()
    assert vm.floatingVisible is True
    assert floating.property("visible") is True

    floating.close()
    app.processEvents()
    assert vm.floatingVisible is False
    assert floating.property("visible") is False

    _click(trigger)
    app.processEvents()
    assert vm.floatingVisible is True
    assert floating.property("visible") is True


def test_opening_floating_window_hides_main_window(ui) -> None:
    app, _engine, root, vm = ui
    floating = _child(root, "floatingAdviceWindow")
    _click(_child(root, "floatingButton"))
    app.processEvents()

    assert vm.floatingVisible is True
    assert floating.property("visible") is True
    assert root.property("visible") is False

    floating.close()
    app.processEvents()
    assert root.property("visible") is True


def test_debug_capture_switch_emits_setting_change(ui) -> None:
    app, _engine, root, vm = ui
    vm.navigate("settings")
    app.processEvents()
    switch = _child(root, "debugCaptureSwitch")
    values: list[bool] = []
    vm.debugCaptureRequested.connect(values.append)

    assert switch.property("checked") is False
    _click(switch)
    app.processEvents()

    assert values == [True]
    assert switch.property("checked") is True

def test_start_button_requests_start_when_certificate_is_trusted(ui) -> None:
    app, _engine, root, vm = ui
    requests: list[bool] = []
    vm.startRequested.connect(lambda: requests.append(True))
    vm.apply_status({"certificate_state": "trusted"})
    app.processEvents()

    _click(_child(root, "startButton"))
    app.processEvents()
    assert requests == [True]
    assert not _child(root, "consentDialog").property("opened")


def test_start_button_opens_consent_when_certificate_is_not_trusted(ui) -> None:
    app, _engine, root, vm = ui
    requests: list[bool] = []
    vm.startRequested.connect(lambda: requests.append(True))

    _click(_child(root, "startButton"))
    app.processEvents()
    assert requests == []
    assert _child(root, "consentDialog").property("opened") is True


def test_authorize_button_emits_true_only_after_checkbox_consent(ui) -> None:
    app, _engine, root, vm = ui
    values: list[bool] = []
    vm.authorizeRequested.connect(values.append)
    consent = _child(root, "certificateConsent")
    authorize = _child(root, "authorizeButton")

    assert authorize.property("enabled") is False
    consent.setProperty("checked", True)
    app.processEvents()
    assert authorize.property("enabled") is True
    _click(authorize)
    app.processEvents()
    assert values == [True]


def test_visual_screenshot_is_available_when_requested(ui, tmp_path: Path) -> None:
    """Optional artifact for local GUI review; does not affect normal CI."""
    app, _engine, root, _vm = ui
    directory = os.environ.get("GUANCE_QML_SCREENSHOT_DIR")
    if not directory:
        pytest.skip("set GUANCE_QML_SCREENSHOT_DIR to save a visual artifact")
    app.processEvents()
    screen = QGuiApplication.primaryScreen()
    assert screen is not None
    image = screen.grabWindow(root.winId())
    assert not image.isNull()
    destination = Path(directory) / "guance-main.png"
    destination.parent.mkdir(parents=True, exist_ok=True)
    assert image.save(str(destination))


def test_game_and_floating_surfaces_keep_key_information_in_demo(ui) -> None:
    app, _engine, root, vm = ui
    vm.enable_demo()
    app.processEvents()

    board = _child(root, "publicGameBoard")
    seats = _child(root, "gameOverviewSeats")
    floating = _child(root, "floatingAdviceWindow")
    assert board.property("visible") is True
    assert seats.property("visible") is True

    _click(_child(root, "floatingButton"))
    app.processEvents()
    floating_board = _child(root, "floatingGameBoard")
    floating_grid = _child(root, "floatingRecentPlayGrid")
    assert floating.property("visible") is True
    assert floating_board.property("visible") is True
    assert floating_grid.property("visible") is True
    assert float(floating.property("width")) >= float(floating.property("minimumWidth"))
    floating.close()


def test_diagnostics_action_row_is_present_and_right_aligned(ui) -> None:
    app, _engine, root, vm = ui
    vm.navigate("diagnostics")
    app.processEvents()
    action_row = _child(root, "diagnosticsActionRow")
    export_button = _child(root, "exportDiagnosticsButton")
    assert action_row.property("visible") is True
    assert export_button.property("x") >= action_row.property("x")
    root.hide()


def test_diagnostics_actions_provide_visible_feedback(ui) -> None:
    app, _engine, root, vm = ui
    vm.navigate("diagnostics")
    app.processEvents()

    feedback = _child(root, "diagnosticsActionFeedback")
    assert feedback.property("visible") is False

    _click(_child(root, "exportDiagnosticsButton"))
    app.processEvents()
    assert feedback.property("visible") is True
    assert "导出" in str(feedback.property("text"))

    vm.apply_status({"phase": "error", "error_message": "同步失败"})
    app.processEvents()
    _click(_child(root, "clearErrorButton"))
    app.processEvents()
    assert feedback.property("visible") is True
    assert "清除" in str(feedback.property("text"))


def test_floating_surface_exposes_unified_game_metrics(ui) -> None:
    app, _engine, root, vm = ui
    vm.enable_demo()
    app.processEvents()
    _click(_child(root, "floatingButton"))
    app.processEvents()

    metrics = _child(root, "floatingGameMetrics")
    assert metrics.property("visible") is True
    assert _child(root, "floatingGameBoard").property("visible") is True
    assert _child(root, "floatingRecentPlayGrid").property("visible") is True


def test_floating_board_scrolls_all_five_eight_and_ten_cards(ui) -> None:
    app, _engine, root, vm = ui
    _click(_child(root, "floatingButton"))

    for count in (5, 8, 10):
        cards = list(full_deck()[:count])
        vm.apply_observation({
            "session_id": "s", "game_id": f"cards-{count}", "state_version": count,
            "level": 5, "my_seat": 0, "current_seat": 0, "hand": [0x15],
            "remaining": [1, 20, 20, 20],
            "history": [{"index": 1, "player": 0, "kind": "play", "cards": cards, "trick": 1}],
        })
        app.processEvents()
        grid = _child(root, "floatingRecentPlayGrid")
        assert grid.property("visible") is True
        assert len(vm.recentPlays[0]["cards"]) == count


def test_end_state_is_visible_and_next_observation_restores_floating_board(ui) -> None:
    app, _engine, root, vm = ui
    _click(_child(root, "floatingButton"))
    vm.apply_observation({
        "session_id": "s", "game_id": "first", "state_version": 1,
        "level": 5, "my_seat": 0, "current_seat": 0, "hand": [0x15],
        "remaining": [1, 20, 20, 20],
    })
    app.processEvents()
    board = _child(root, "floatingGameBoard")
    assert board.property("visible") is True

    vm.apply_status({"phase": "ended"})
    app.processEvents()
    assert vm.observationAvailable is False
    assert board.property("visible") is False
    assert "本局已结束" in str(_child(root, "floatingAdviceWindow").property("title")) or vm.statusTitle == "本局已结束"

    vm.apply_observation({
        "session_id": "s", "game_id": "second", "state_version": 1,
        "level": 6, "my_seat": 0, "current_seat": 0, "hand": [0x31, 0x32],
        "remaining": [2, 20, 20, 20],
    })
    app.processEvents()
    assert vm.observationAvailable is True
    assert board.property("visible") is True


def test_error_text_is_selectable_without_copy_buttons_and_directories_stay_wired(ui) -> None:
    app, _engine, root, vm = ui
    vm.apply_status({"phase": "error", "error_message": "同步失败\n详细原因"})
    app.processEvents()

    # Copy is provided by normal text selection/Ctrl+C/right-click, not by
    # adding more action buttons to either page.
    for name in ("copyStatusErrorButton", "copyDiagnosticsErrorButton", "copyDiagnosticButton"):
        assert _maybe_child(root, name) is None

    assistant_error = _child(root, "statusErrorText")
    assert assistant_error.property("readOnly") is True
    assert assistant_error.property("selectByMouse") is True
    assert assistant_error.property("persistentSelection") is True
    assert assistant_error.findChild(QObject, "selectableTextPointerSurface") is not None

    selectable_source = Path(__file__).parents[2] / "src" / "guance" / "presentation" / "qml" / "SelectableText.qml"
    source = selectable_source.read_text(encoding="utf-8")
    assert "onDoubleTapped" in source
    assert "control.selectAll()" in source
    # Native synthetic mouse double-clicks can crash the Windows offscreen
    # TextArea backend. Invoke the same QML helper used by TapHandler instead;
    # this verifies the production select-all path without a platform crash.
    assert QMetaObject.invokeMethod(assistant_error, "selectAllText", Qt.ConnectionType.DirectConnection)
    app.processEvents()
    assert assistant_error.property("selectedText") == assistant_error.property("text")

    # Native keyboard copy must still target the focused, read-only editor.
    app.clipboard().clear()
    QTest.keyClick(root, Qt.Key_C, Qt.ControlModifier)
    assert app.clipboard().text() == assistant_error.property("text")

    vm.navigate("diagnostics")
    vm.apply_status({"phase": "error", "error_message": "同步失败\n详细原因",
                     "diagnostics": {"命令": "3007"}})
    app.processEvents()
    assert _child(root, "diagnosticsList").property("visible") is True

    summary_error = _child(root, "diagnosticsErrorText")
    assert summary_error.property("readOnly") is True
    assert summary_error.property("selectByMouse") is True
    assert summary_error.findChild(QObject, "selectableTextPointerSurface") is not None

    # Repeater delegates are visual children of the list and are not always
    # QObject descendants of the window, so inspect the named entry directly.
    diagnostics_list = root.findChild(QQuickItem, "diagnosticsList")
    assert diagnostics_list is not None
    entries = [item for item in diagnostics_list.childItems()
               if item.objectName() == "diagnosticEntry"]
    assert entries
    detail_value = entries[0].findChild(QObject, "diagnosticValueText")
    assert detail_value is not None
    assert detail_value.property("readOnly") is True
    assert detail_value.property("selectByMouse") is True
    assert detail_value.findChild(QObject, "selectableTextPointerSurface") is not None

    directory_requests: list[str] = []
    vm.openDiagnosticsDirectoryRequested.connect(lambda: directory_requests.append("exports"))
    vm.openCaptureDirectoryRequested.connect(lambda: directory_requests.append("captures"))
    _click(_child(root, "openDiagnosticsDirectoryButton"))
    _click(_child(root, "openCaptureDirectoryButton"))
    assert directory_requests == ["exports", "captures"]


def _walk_quick_items(item: QQuickItem):
    yield item
    for child in item.childItems():
        yield from _walk_quick_items(child)


def _quick_named(item: QQuickItem, name: str) -> QQuickItem | None:
    for child in _walk_quick_items(item):
        if child.objectName() == name:
            return child
    return None


def _table_fixture(card_count: int = 27) -> dict:
    ranks = list(range(1, 14)) + [14, 15]
    return {
        "session_id": "ui",
        "game_id": "table-ui",
        "state_version": 1,
        "phase": "ready",
        "level": 5,
        "my_seat": 2,
        "seat_confirmed": True,
        "first_seat": 2,
        "current_seat": 2,
        "finish_ranks_by_seat": [-1, 2, 1, -1],
        "hand": [0x15, 0x25],
        "remaining": [20, 20, 20, 20],
        "history": [{"player": 2, "kind": "play", "cards": list(full_deck()[:card_count]), "trick": 1}],
        "card_counter": [{"rank": rank, "total": 4, "played": 1, "in_hand": 0, "unseen": 3, "is_level": rank == 5} for rank in ranks],
        "counter_unseen_total": 45,
    }


def test_table_snapshot_renders_counterclockwise_rows_counter_and_wrapped_cards(ui) -> None:
    app, _engine, root, vm = ui
    vm.apply_table_state(_table_fixture())
    app.processEvents()

    seats = root.findChild(QQuickItem, "gameOverviewSeats")
    assert seats is not None and seats.property("visible") is True
    rows = [child for child in _walk_quick_items(seats) if child.objectName().startswith("seatPlayRow-")]
    assert [row.objectName() for row in rows] == ["seatPlayRow-2", "seatPlayRow-1", "seatPlayRow-0", "seatPlayRow-3"]
    assert _quick_named(rows[0], "finishRankBadge-2").property("visible") is True
    assert _quick_named(rows[1], "finishRankBadge-1").property("visible") is True
    active_flow = _quick_named(rows[0], "seatPlayFlow-2")
    assert active_flow is not None
    cards = [child for child in active_flow.childItems() if "CardFace" in child.metaObject().className()]
    assert len(cards) == 27
    assert active_flow.height() > cards[0].height()
    assert all(card.x() >= -0.5 and card.x() + card.width() <= active_flow.width() + 0.5 for card in cards)

    counter = root.findChild(QQuickItem, "mainCardCounter")
    assert counter is not None and counter.property("visible") is True
    grid = _quick_named(counter, "cardCounterGrid")
    assert grid is not None
    cells = [child for child in grid.childItems() if child.objectName().startswith("cardCounterCell-")]
    assert len(cells) == 15

    _click(_child(root, "floatingButton"))
    app.processEvents()
    floating = _child(root, "floatingAdviceWindow")
    for width in (400, 560, 800):
        floating.setProperty("width", width)
        app.processEvents()
        floating_rows_root = root.findChild(QQuickItem, "floatingRecentPlayGrid")
        assert floating_rows_root is not None
        floating_rows = [child for child in _walk_quick_items(floating_rows_root) if child.objectName().startswith("seatPlayRow-")]
        assert len(floating_rows) == 4
        flow = _quick_named(floating_rows[0], "seatPlayFlow-2")
        assert flow is not None
        cards = [child for child in flow.childItems() if "CardFace" in child.metaObject().className()]
        assert len(cards) == 27
        assert flow.height() > cards[0].height()
        assert all(card.x() >= -0.5 and card.x() + card.width() <= flow.width() + 0.5 for card in cards)
    assert floating.property("minimumWidth") == 400
    assert floating.property("minimumHeight") >= 200


def test_floating_header_close_icon_is_shared_and_counter_is_pinned(ui) -> None:
    app, _engine, root, vm = ui
    vm.apply_table_state(_table_fixture(8))
    _click(_child(root, "floatingButton"))
    app.processEvents()

    floating = _child(root, "floatingAdviceWindow")
    close_button = _child(root, "floatingCloseButton")
    middle = _child(root, "floatingMiddleScroll")
    counter = _child(root, "floatingCardCounter")
    assert close_button.property("text") == "×"
    assert middle.property("objectName") == "floatingMiddleScroll"
    assert counter.property("visible") is True
    assert counter.property("y") > middle.property("y")

    _click(close_button)
    app.processEvents()
    assert vm.floatingVisible is False
    assert floating.property("visible") is False
    assert root.property("visible") is True
    assert _child(root, "floatingLayoutSaveTimer").property("running") is False
    assert _child(root, "floatingFitTimer").property("running") is False
    _settle(app)
    assert floating.property("visible") is False


def test_floating_play_cards_render_rank_and_suit_for_eight_valid_cards(ui) -> None:
    app, _engine, root, vm = ui
    table = _table_fixture(8)
    table["history"] = [{
        "player": 2,
        "kind": "play",
        "cards": [0x11, 0x12, 0x21, 0x22, 0x31, 0x32, 0x41, 0x42],
        "trick": 1,
    }]
    vm.apply_table_state(table)
    _click(_child(root, "floatingButton"))
    app.processEvents()

    rows_root = _child(root, "floatingRecentPlayGrid")
    flow = _quick_named(rows_root, "seatPlayFlow-2")
    assert flow is not None
    cards = [child for child in flow.childItems() if "CardFace" in child.metaObject().className()]
    assert len(cards) == 8
    assert all(card.y() == 0 for card in cards)
    for card in cards:
        rank = _quick_named(card, "cardRank")
        suit = _quick_named(card, "cardSuit")
        assert rank is not None and suit is not None
        assert rank.property("visible") is True
        assert suit.property("visible") is True
        assert str(rank.property("text"))
        assert str(suit.property("text"))
        for text in (rank, suit):
            assert 0 <= text.x() and text.x() + text.width() <= card.width()
            assert 0 <= text.y() and text.y() + text.height() <= card.height()
        assert rank.y() + rank.height() <= suit.y()
        assert card.x() + card.width() <= flow.width()


def test_floating_board_switches_between_two_columns_and_one_column(ui) -> None:
    app, _engine, root, vm = ui
    vm.apply_table_state(_table_fixture(8))
    _click(_child(root, "floatingButton"))
    app.processEvents()
    layout = _quick_named(_child(root, "floatingRecentPlayGrid"), "seatPlayLayout")
    assert layout is not None
    floating = _child(root, "floatingAdviceWindow")
    floating.setProperty("width", 560)
    app.processEvents()
    assert layout.property("columns") == 2
    floating.setProperty("width", 400)
    app.processEvents()
    assert layout.property("columns") == 1


def test_floating_fit_grows_only_and_manual_close_flushes_geometry(ui, tmp_path: Path) -> None:
    app, _engine, root, vm = ui
    table = _table_fixture(8)
    deck = full_deck()
    table["hand"] = list(deck[:27])
    table["history"] = [{"player": seat, "kind": "play", "cards": list(deck[27 + seat * 8:35 + seat * 8]), "trick": 1} for seat in range(4)]
    vm.apply_status({"phase": "ready", "running": True})
    vm.apply_table_state(table)
    stopped = []
    vm.stopRequested.connect(lambda: stopped.append(True))
    _click(_child(root, "floatingButton"))
    _settle(app)
    floating = _child(root, "floatingAdviceWindow")
    assert floating.transientParent() is None
    assert floating.property("sizeMode") == "fit"
    assert floating.width() == 560
    bounds = vm.screen_work_area(floating)
    assert floating.x() >= bounds["x"] and floating.y() >= bounds["y"]
    assert floating.x() + floating.width() <= bounds["x"] + bounds["width"]
    assert floating.y() + floating.height() <= bounds["y"] + bounds["height"]
    height = floating.height()
    middle = _child(root, "floatingMiddleScroll")
    counter = _child(root, "floatingCardCounter")
    header = _child(root, "floatingHeader")
    assert header.y() + header.height() <= middle.y()
    assert middle.y() + middle.height() <= counter.y()
    assert counter.y() + counter.height() <= floating.height()
    assert floating.grabWindow().save(str(tmp_path / "floating-full-pressure.png"))
    print(f"Floating screenshot: {tmp_path / 'floating-full-pressure.png'}")

    table["hand"] = table["hand"][:1]
    table["history"] = []
    table["state_version"] += 1
    vm.apply_table_state(table)
    _settle(app)
    assert floating.height() == height
    _click(_child(root, "floatingAutoFitMenuItem"))
    _settle(app)
    assert floating.height() < height

    floating.setWidth(480)
    floating.setHeight(420)
    assert floating.property("sizeMode") == "manual"
    _click(_child(root, "floatingCloseButton"))
    assert vm.floatingLayout["width"] == 480
    assert vm.floatingLayout["height"] == 420
    assert vm.floatingLayout["sizeMode"] == "manual"
    _settle(app)
    assert not floating.isVisible() and root.isVisible()
    assert vm.running and not stopped
    _click(_child(root, "floatingButton"))
    _settle(app)
    assert (floating.width(), floating.height()) == (480, 420)
    assert not _child(root, "floatingFitTimer").property("running")
    assert not _child(root, "floatingLayoutSaveTimer").property("running")


def test_finish_badge_requires_valid_rank_and_keeps_rank_four_readable(ui) -> None:
    app, _engine, root, vm = ui
    table = _table_fixture()
    table["finish_ranks_by_seat"] = [4, 0, 5, -1]
    vm.apply_table_state(table)
    app.processEvents()

    seats = root.findChild(QQuickItem, "gameOverviewSeats")
    assert seats is not None
    rows = {
        row.objectName(): row
        for row in _walk_quick_items(seats)
        if row.objectName().startswith("seatPlayRow-")
    }
    invalid_badge = _quick_named(rows["seatPlayRow-1"], "finishRankBadge-1")
    assert invalid_badge is not None
    assert invalid_badge.property("visible") is False

    rank_four_badge = _quick_named(rows["seatPlayRow-0"], "finishRankBadge-0")
    assert rank_four_badge is not None
    assert rank_four_badge.property("visible") is True
    rank_four_text = rank_four_badge.findChild(QObject)
    assert rank_four_text is not None
    assert rank_four_text.property("text") == "末游"
    assert rank_four_text.property("color") != rank_four_badge.property("color")
    source = (Path(__file__).parents[2] / "src" / "guance" / "presentation"
              / "qml" / "SeatPlayRows.qml").read_text(encoding="utf-8")
    assert 'modelData.finishRank === 4 ? Theme.ink : "#17202A"' in source


def test_floating_menu_close_restores_main_and_reopens(ui) -> None:
    app, _engine, root, vm = ui
    floating = _child(root, "floatingAdviceWindow")
    _click(_child(root, "floatingButton"))
    app.processEvents()
    assert vm.floatingVisible is True
    assert floating.property("visible") is True

    # Native close remains equivalent to the fixed in-window close control.
    _click(_child(root, "floatingMenuButton"))
    assert root.findChild(QObject, "closeFloatingMenuItem") is None
    floating.close()
    app.processEvents()
    assert vm.floatingVisible is False
    assert floating.property("visible") is False
    assert root.property("visible") is True

    _click(_child(root, "floatingButton"))
    app.processEvents()
    assert vm.floatingVisible is True
    assert floating.property("visible") is True

def test_floating_menu_manual_seat_confirmation_is_visible_only_before_confirmation(ui) -> None:
    app, _engine, root, vm = ui
    vm.apply_table_state({
        "session_id": "s", "game_id": "manual-seat", "state_version": 1,
        "phase": "confirming_seat", "level": 5, "my_seat": -1,
        "seat_confirmed": False, "first_seat": 1, "current_seat": 1,
        "hand": [0x15], "remaining": [27, 27, 27, 27], "history": [],
    })
    app.processEvents()
    _click(_child(root, "floatingButton"))
    _click(_child(root, "floatingMenuButton"))
    menu = _child(root, "confirmSeatMenu")
    assert menu.property("visible") is True
    assert menu.property("enabled") is True
    assert _child(root, "confirmSeatSelfMenuItem").property("text") == "首家是我"
    assert _child(root, "confirmSeatLeftMenuItem").property("text") == "首家是左家"
    assert _child(root, "confirmSeatPartnerMenuItem").property("text") == "首家是对家"
    assert _child(root, "confirmSeatRightMenuItem").property("text") == "首家是右家"
    assert all(str(_child(root, name).property("text")).find("座位") < 0 for name in (
        "confirmSeatSelfMenuItem", "confirmSeatLeftMenuItem",
        "confirmSeatPartnerMenuItem", "confirmSeatRightMenuItem"))

    requested: list[str] = []
    vm.confirmSeatRequested.connect(requested.append)
    _click(_child(root, "confirmSeatRightMenuItem"))
    assert requested == ["right"]

    vm.apply_table_state({
        "session_id": "s", "game_id": "manual-seat", "state_version": 2,
        "phase": "active", "level": 5, "my_seat": 2,
        "seat_confirmed": True, "first_seat": 1, "current_seat": 2,
        "hand": [0x15], "remaining": [27, 27, 27, 27], "history": [],
    })
    app.processEvents()
    assert menu.property("visible") is False
    assert menu.property("enabled") is False
