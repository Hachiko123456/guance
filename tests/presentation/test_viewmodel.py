"""Presentation-only tests; no network, proxy, or Windows certificate operations."""
from __future__ import annotations

from PySide6.QtCore import QObject, QMetaObject, Qt
from PySide6.QtWidgets import QApplication

from guance.presentation.viewmodel import GuanceViewModel


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
    assert "重新打开" in vm.statusDetail


def invoke_click(object_: QObject) -> None:
    assert QMetaObject.invokeMethod(object_, "click", Qt.ConnectionType.DirectConnection)
