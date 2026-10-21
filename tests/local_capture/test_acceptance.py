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

# Q1=A: the five legacy captures are fixed, read-only, deterministic negative
# regressions. The rules/runtime source and bundle used for the model runtime are
# the frozen Phase 2 materials; the live Guandan worktree is never a trusted
# rules/runtime root.
FROZEN_SOURCE = (ROOT / "build" / "acceptance" / "guance-model-platform-20261016"
                 / "phase2" / "runtime" / "marvel-e5c82e7c-d7acbc8d" / "source")
CHECKPOINT = Path(
    r"D:\project\guandan\runs\comparison_delivery\dataset\m09_candidate_iter002000.pt")
MODEL_MANIFEST = (ROOT / "build" / "acceptance" / "guance-model-platform-20261016"
                  / "phase2" / "bundles" / "marvel-v2-iter002000" / "manifest.json")
EXPECTED_CHECKPOINT_SHA256 = (
    "af0df1eaeb67c88e467ee78bf6b0494c49d000f5733d655ee81b40351d4ca9c8")


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
def test_selected_capture_is_read_only_and_rejected_negative_regression(
        selector: dict[str, object]) -> None:
    """Q1=A negative regression for the five legacy captures.

    Each capture is a fixed, read-only, deterministic negative: the file bytes
    and fixed SHA-256 are unchanged and the converter rejects it with
    ``event_unsupported``. The historical old-contract decision counts
    (20/21/17/21/24) are retained only as historical records and are never used
    as a current positive gate.
    """
    path = (ROOT / str(selector["relative_path"])).resolve()
    assert path.is_file(), f"missing selected capture: {selector['capture_id']}"
    fixed_sha256 = str(selector["source_sha256"])
    assert len(fixed_sha256) == 64
    before = _hash(path)
    assert before == fixed_sha256
    from guance.infrastructure.training_data.legacy import read_legacy_episodes
    episodes, report = read_legacy_episodes(path)
    after = _hash(path)
    assert before == after == fixed_sha256  # byte-for-byte unchanged
    assert report["status"] == "rejected"
    codes = [error.get("code") for error in report["errors"]]
    assert "event_unsupported" in codes, report["errors"]
    assert report["accepted_candidates"] == 0
    assert report["partial"] == 0
    assert report["quarantined"] == 0
    # No converted records: no recommendation, no fallback, no field synthesis.
    assert sum(len(episode.get("records", [])) for episode in episodes) == 0
    # Historical old-contract count retained only as a record; not a gate.
    assert int(selector["expected_decision_count"]) > 0


@pytest.mark.local_capture
def test_fixed_capture_acceptance_is_read_only_and_reports_exact_negative_blockers() -> None:
    """The subprocess gate over the five legacy captures must be an exact negative.

    Uses the frozen Phase 2 runtime source and the current-schema Phase 2 bundle
    (never the live Guandan worktree). The runtime loads fail-closed to
    ``unavailable`` / ``GUANDAN_STATE_UNAVAILABLE`` and every capture is a
    deterministic ``event_unsupported`` rejection with zero recommendations.
    """
    assert MODEL_MANIFEST.is_file(), "missing explicit Phase 2 model manifest"
    assert FROZEN_SOURCE.is_dir(), "missing frozen Phase 2 runtime source"
    assert CHECKPOINT.is_file(), "missing fixed checkpoint"
    before = _capture_files()
    command = [
        sys.executable, "-B", str(ROOT / "scripts" / "run_local_capture_acceptance.py"),
        "--root", str(ROOT), "--model-manifest", str(MODEL_MANIFEST),
        "--model-source", str(FROZEN_SOURCE), "--checkpoint", str(CHECKPOINT),
    ]
    assert str(FROZEN_SOURCE) != r"D:\project\guandan"
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                               encoding="utf-8", errors="strict", timeout=180,
                               env=os.environ | {"PYTHONDONTWRITEBYTECODE": "1"})
    assert completed.returncode == 2, completed.stderr
    assert not completed.stderr
    result = json.loads(completed.stdout)
    assert result["schema"] == "guance-gmv2-phase7-local-capture-summary-v1"
    assert result["status"] == "blocked"
    runtime = result["runtime"]
    assert runtime["status"] == "unavailable"
    assert runtime["error_code"] == "GUANDAN_STATE_UNAVAILABLE"
    assert runtime["provider"] == "guandan_marvel_v2"
    assert runtime["model_sha256"] == EXPECTED_CHECKPOINT_SHA256
    assert isinstance(runtime["bundle_fingerprint"], str) and runtime["bundle_fingerprint"]
    assert isinstance(runtime["deployment_fingerprint"], str) and runtime["deployment_fingerprint"]
    assert runtime["production_ready"] is False
    assert runtime["strength_claim"] is False
    assert runtime["public_self_supported"] is False
    totals = result["totals"]
    assert totals["expected_decisions"] == 103
    assert totals["evaluated_decisions"] == 0
    assert totals["unevaluated_decisions"] == 103
    assert totals["supported_decisions"] == 0
    assert totals["captures_passed"] == 0
    assert totals["captures_blocked"] == 5
    assert [item["capture_id"] for item in result["captures"]] == [
        selector["capture_id"] for selector in SELECTORS
    ]
    for index, (item, selector) in enumerate(zip(result["captures"], SELECTORS, strict=True)):
        assert item["source_sha256"] == selector["source_sha256"]
        assert item["expected_decisions"] == selector["expected_decision_count"]
        assert item["source_unchanged"] is True
        assert item["directory_unchanged"] is True
        assert item["status"] == "blocked"
        assert item["evaluated_decisions"] == 0
        assert item["model_inferences"] == 0
        assert item["recommendations"] == 0
        assert item["invalid_recommendations"] == 0
        codes = item["unsupported_codes"]
        assert codes["LOCAL_CAPTURE_CONVERSION_REJECTED"] == 1
        assert codes["LOCAL_CAPTURE_DECISIONS_UNAVAILABLE"] == selector["expected_decision_count"]
        assert item["capture_closed_verified"] is (index >= 3)
        if index < 3:
            assert codes["CAPTURE_CLOSED_SIDECAR_MISSING"] == 1
    text = completed.stdout.lower()
    assert r"d:\project" not in text
    assert "hidden_opponent" not in text
    assert '"cards"' not in text and '"hand"' not in text and '"history"' not in text
    assert _capture_files() == before
