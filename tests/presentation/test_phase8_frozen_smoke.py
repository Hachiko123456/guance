"""Phase 8 bounded source/frozen GUI acceptance-smoke contract."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["QT_QUICK_BACKEND"] = "software"
    env["GUANCE_DATA_DIR"] = str(tmp_path / "data")
    return env


def test_bootstrap_smoke_import_is_lazy_for_external_marvel_runtime(tmp_path: Path) -> None:
    script = r"""
import sys
import guance.bootstrap
assert 'torch' not in sys.modules
assert 'guandan_marvel' not in sys.modules
assert 'nanopokerlib' not in sys.modules
assert 'guance.infrastructure.model.marvel_v2.runtime' not in sys.modules
assert 'guance.infrastructure.model.marvel_v2.deployment_bundle' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script], cwd=ROOT, env=_env(tmp_path),
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="Phase 8 target is physical Windows")
def test_bounded_no_network_qml_smoke_writes_report_and_screenshot(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    screenshot = tmp_path / "screenshot.png"
    result = subprocess.run(
        [
            sys.executable, "-B", "-m", "guance", "--frozen-smoke",
            "--smoke-state", "unavailable", "--smoke-theme", "dark",
            "--smoke-report", str(report), "--screenshot", str(screenshot),
            "--exit-after", "6",
        ],
        cwd=ROOT, env=_env(tmp_path), capture_output=True, text=True,
        timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["status"] == "passed"
    assert payload["no_network_mode"] is True
    assert payload["controller_created"] is False
    assert payload["proxy_or_certificate_operations"] is False
    assert payload["qtcore_imported"] is True
    assert payload["qtqml_imported"] is True
    assert payload["qtquick_imported"] is True
    assert payload["qml_engine_created"] is True
    assert payload["main_window_created"] is True
    assert payload["root_visible"] is True
    assert payload["state"] == "unavailable"
    assert payload["theme"] == "dark"
    assert not any(payload["module_guards"].values())
    assert payload["screenshot_saved"] is True
    assert screenshot.is_file() and screenshot.stat().st_size > 0


def test_frozen_no_capture_status_is_explicitly_unavailable(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from guance.bootstrap import _capture_helper_status

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    status, helper = _capture_helper_status({"runtime_dir": tmp_path / "runtime"})

    assert status == "unavailable"
    assert helper == ""
