from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).parents[2]
SELECTORS = json.loads((ROOT / "tests" / "local_capture_manifest.json").read_text(encoding="utf-8"))
GUANDAN = Path(r"D:\project\guandan")
CHECKPOINT = GUANDAN / (
    "runs/M09_full_training/remote/ippo_pilot_failed_20261011/"
    "extracted-output/evaluation_checkpoints/iter000100.pt"
)
MODEL_MANIFEST = ROOT / (
    "build/acceptance/guandan-marvel-v2-phase6-20261013/"
    "experimental-bundle/manifest.json"
)


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _capture_files() -> dict[str, tuple[int, str]]:
    files: dict[str, tuple[int, str]] = {}
    for selector in SELECTORS:
        directory = (ROOT / selector["relative_path"]).parent
        assert directory.is_dir(), f"missing selected capture directory: {selector['capture_id']}"
        for path in directory.iterdir():
            if path.is_file():
                files[str(path.resolve())] = (path.stat().st_size, _hash(path))
    return files


@pytest.mark.local_capture
@pytest.mark.parametrize("selector", SELECTORS, ids=lambda item: item["capture_id"])
def test_selected_capture_is_read_only_and_accepted(selector: dict[str, object]) -> None:
    """Preserve the original selector/decision acceptance gate without downgrading it."""
    path = (ROOT / str(selector["relative_path"])).resolve()
    assert path.is_file(), f"missing selected capture: {selector['capture_id']}"
    before = _hash(path)
    assert before == selector["source_sha256"]
    from guance.infrastructure.training_data.legacy import read_legacy_episodes
    episodes, report = read_legacy_episodes(path)
    after = _hash(path)
    assert before == after
    assert report["status"] == "converted"
    assert report["errors"] == []
    assert report["accepted_candidates"] == selector["expected_decision_count"]
    assert report["partial"] == 0
    assert report["quarantined"] == 0
    assert len(episodes) == 1


@pytest.mark.local_capture
def test_phase7_fixed_capture_acceptance_is_read_only_and_reports_exact_blockers() -> None:
    assert MODEL_MANIFEST.is_file(), "missing explicit Phase 6 model manifest"
    assert GUANDAN.is_dir(), "missing fixed read-only Guandan source"
    assert CHECKPOINT.is_file(), "missing fixed checkpoint"
    before = _capture_files()
    command = [
        sys.executable, "-B", str(ROOT / "scripts" / "run_local_capture_acceptance.py"),
        "--root", str(ROOT), "--model-manifest", str(MODEL_MANIFEST),
        "--model-source", str(GUANDAN), "--checkpoint", str(CHECKPOINT),
    ]
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                               encoding="utf-8", errors="strict", timeout=120,
                               env=os.environ | {"PYTHONDONTWRITEBYTECODE": "1"})
    assert completed.returncode == 2, completed.stderr
    assert not completed.stderr
    result = json.loads(completed.stdout)
    assert result["schema"] == "guance-gmv2-phase7-local-capture-summary-v1"
    assert result["status"] == "blocked"
    assert result["runtime"]["status"] == "unavailable"
    assert result["runtime"]["provider"] == "guandan_marvel_v2"
    assert result["runtime"]["model_sha256"] == (
        "44b8a41371965279c7ce8f3e592162689e9a1b32cb4a07147f2a8ea58ff89696"
    )
    assert result["runtime"]["production_ready"] is False
    assert result["runtime"]["strength_claim"] is False
    assert result["runtime"]["public_self_supported"] is False
    assert result["totals"]["expected_decisions"] == 103
    assert result["totals"]["evaluated_decisions"] == 0
    assert result["totals"]["unevaluated_decisions"] == 103
    assert result["totals"]["captures_passed"] == 0
    assert result["totals"]["captures_blocked"] == 5
    assert [item["capture_id"] for item in result["captures"]] == [
        selector["capture_id"] for selector in SELECTORS
    ]
    for index, (item, selector) in enumerate(zip(result["captures"], SELECTORS, strict=True)):
        assert item["source_sha256"] == selector["source_sha256"]
        assert item["expected_decisions"] == selector["expected_decision_count"]
        assert item["source_unchanged"] is True
        assert item["directory_unchanged"] is True
        assert item["status"] == "blocked"
        assert item["unsupported_codes"]["LOCAL_CAPTURE_CONVERSION_REJECTED"] == 1
        assert item["unsupported_codes"]["CAPTURE_PROTOCOL_REJECTED"] == 1
        assert item["unsupported_codes"]["LOCAL_CAPTURE_DECISIONS_UNAVAILABLE"] == selector["expected_decision_count"]
        assert item["capture_closed_verified"] is (index >= 3)
        if index < 3:
            assert item["unsupported_codes"]["CAPTURE_CLOSED_SIDECAR_MISSING"] == 1
    text = completed.stdout.lower()
    assert r"d:\project" not in text
    assert "hidden_opponent" not in text
    assert '"cards"' not in text and '"hand"' not in text and '"history"' not in text
    assert _capture_files() == before
