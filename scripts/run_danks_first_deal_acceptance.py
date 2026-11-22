"""Replay an observed deal through the live controller, frozen DanKS worker and packaged QML."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtQuick import QQuickItem
from PySide6.QtWidgets import QApplication

from guance.bootstrap import create_live_controller
from guance.config import app_paths, ensure_app_paths
from guance.infrastructure.capture.offline_replay import iter_debug_frames, sha256_file
from guance.infrastructure.model.client import default_client_factory
from guance.presentation.app import create_ui
from guance.presentation.viewmodel import GuanceViewModel


def wait_for(app, condition, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return False


def run(args, data_dir, *, model_client_factory=None, qml_root=None):
    executable = args.package_root / "Guance.exe"
    internal = args.package_root / "_internal"
    app = QApplication.instance() or QApplication([])
    os.environ["GUANCE_DATA_DIR"] = str(data_dir)
    os.environ["GUANCE_TRAINING_DATA"] = "0"
    paths = app_paths()
    paths["root_dir"] = internal
    paths["runtime_dir"] = internal / "runtime"
    ensure_app_paths(paths)

    def frozen_worker(source, **options):
        client = default_client_factory(source, **options)
        worker_args = [str(executable), "--model-worker", "--threads", str(options["threads"]),
                       "--device", "cpu"]
        client._worker_process_spec = lambda: (worker_args, os.environ.copy(), subprocess.CREATE_NO_WINDOW)
        return client

    vm = GuanceViewModel()
    controller = create_live_controller(vm, paths, model_client_factory=model_client_factory or frozen_worker)
    engine = create_ui(app, vm, qml_root=qml_root or internal / "guance" / "presentation" / "qml")
    root = engine.rootObjects()[0]
    turns = {}
    frame_count = 0
    try:
        controller._bind_recommendation_session()
        controller._model.start()
        if not wait_for(app, lambda: vm.modelState in {"ready", "error"}, 75) or vm.modelState != "ready":
            raise RuntimeError("DanKS did not become ready")
        if vm.modelMetadata.get("provider") != "danks_v3":
            raise RuntimeError("wrong model provider")

        for frame_count, record in enumerate(iter_debug_frames(args.frames), 1):
            controller._on_frame(record.frame)
            app.processEvents()
            observation = controller.session.latest_observation
            if observation is None or observation.my_seat != observation.current_seat:
                if vm.adviceVisible:
                    raise RuntimeError("advice visible outside the current player's turn")
                continue
            key = observation.key
            turn = turns.setdefault(key, {"version": key[2], "first_frame": frame_count})
            if "submitted" in turn or key not in controller._advice_submitted:
                continue

            def visible_for_turn():
                if not vm.adviceVisible or vm._key(vm._observation) != key:
                    return False
                grid = root.findChild(QQuickItem, "visibleAdviceCandidates")
                return grid is not None and any(
                    item.objectName() == "adviceCandidate-0" and item.isVisible()
                    and item.width() > 0 and item.height() > 0 for item in grid.childItems()
                )

            budget = 6.0 if observation.free_lead else 2.0
            turn["submitted"] = True

            def turn_error():
                return next((item.get("error_code", "") for item in reversed(tuple(controller.log.recent))
                             if item["event"] == "advice_result" and item.get("game_id") == key[1]
                             and item.get("state_version") == key[2]), "")

            # The transport enforces the per-turn budget after sending a
            # request. A replacement worker can need a fresh load first.
            wait_for(app, lambda: visible_for_turn() or bool(turn_error())
                     or vm.modelState in {"error", "timeout"}, budget + 15.0)
            turn["displayed"] = visible_for_turn()
            turn["candidates"] = len(vm.adviceCandidates) if turn["displayed"] else 0
            if not turn["displayed"]:
                turn["model_state"] = vm.modelState
                turn["phase"] = vm.phase
                turn["error_code"] = turn_error() or controller._model.status.error_code
    finally:
        controller.close()
        root.hide()
        app.processEvents()
    return {
        "executable_sha256": sha256_file(executable)[0],
        "capture_sha256": sha256_file(args.frames)[0],
        "frames": frame_count,
        "own_turns": len(turns),
        "submitted": sum(bool(turn.get("submitted")) for turn in turns.values()),
        "displayed": sum(bool(turn.get("displayed")) for turn in turns.values()),
        "turns": list(turns.values()),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-root", required=True, type=lambda path: Path(path).resolve())
    parser.add_argument("--frames", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--expect-turns", type=int, required=True)
    parser.add_argument("--expect-visible", type=int, required=True)
    parser.add_argument("--check-timeout-recovery", action="store_true",
                        help="require all admitted turns to resolve and verify recovery after any timeout")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="guance-first-deal-") as directory:
        report = run(args, Path(directory))
    timeouts = [turn["first_frame"] for turn in report["turns"] if turn.get("error_code") == "MODEL_TIMEOUT"]
    shown = [turn["first_frame"] for turn in report["turns"] if turn.get("displayed")]
    report["timeouts"] = len(timeouts)
    report["passed"] = report["own_turns"] == args.expect_turns and report["submitted"] == args.expect_visible
    if args.check_timeout_recovery:
        report["passed"] &= (len(shown) + len(timeouts) == args.expect_visible
                             and bool(shown) and (not timeouts or max(shown) > min(timeouts)))
    else:
        report["passed"] &= report["displayed"] == args.expect_visible
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"frames={report['frames']} own_turns={report['own_turns']} "
          f"submitted={report['submitted']} displayed={report['displayed']} timeouts={report['timeouts']}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
