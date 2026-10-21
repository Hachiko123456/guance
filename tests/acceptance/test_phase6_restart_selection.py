"""Phase 6 restart-selection fail-closed tests (no model load).

These cover the catalog -> active -> resolve boundary that every independent
restart probe runs before any worker is launched: no active, missing bundle,
partial/temp directories, duplicate model_id and manifest/artifact tamper must
all fail closed with bounded output.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from guance.ports.model import ActiveSelectionDTO  # noqa: E402
from guance.infrastructure.model.errors import ModelBackendError  # noqa: E402
from guance.infrastructure.model.selection import save_active_selection  # noqa: E402
from scripts.run_phase6_restart_probe import run  # noqa: E402
from tests.model.model_bundle_factory import write_bundle  # noqa: E402


def _args(models_root: Path, mode: str = "public") -> argparse.Namespace:
    return argparse.Namespace(models_root=models_root, mode=mode, payload="",
                              fixture=str(ROOT / "tests" / "fixtures" / "observations.json"),
                              device="cpu", timeout=5.0, load_timeout=5.0)


def _models_root(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "appdata" / "models"
    catalog = root / "catalog"
    catalog.mkdir(parents=True)
    return root, catalog, root / "active_model.json"


def _select(active_file: Path, root: Path, model_id: str, bundle: str) -> None:
    save_active_selection(active_file, ActiveSelectionDTO(
        kind="catalog", model_id=model_id, bundle=bundle, selected_at="2026-10-20T00:00:00+00:00"),
        root=root)


def test_no_active_selection_fails_closed(tmp_path):
    root, _catalog, _active = _models_root(tmp_path)
    output, code = run(_args(root))
    assert code == 2
    assert output["status"] == "fail_closed"
    assert output["error_code"] == "NO_ACTIVE_SELECTION"


def test_missing_bundle_fails_closed(tmp_path):
    root, _catalog, active = _models_root(tmp_path)
    _select(active, root, "ghost-model", "ghost-dir")
    output, code = run(_args(root))
    assert code == 2
    assert output["error_code"] == "ACTIVE_SELECTION_INVALID"
    assert output["active"]["model_id"] == "ghost-model"


def test_duplicate_model_id_fails_closed_and_is_reported(tmp_path):
    root, catalog, active = _models_root(tmp_path)
    write_bundle(catalog / "dup-a", model_id="duplicate-model")
    write_bundle(catalog / "dup-b", model_id="duplicate-model")
    _select(active, root, "duplicate-model", "dup-a")
    output, code = run(_args(root))
    assert code == 2
    assert output["error_code"] == "ACTIVE_SELECTION_INVALID"
    codes = {problem["code"] for problem in output["catalog"]["problems"]}
    assert "DUPLICATE_MODEL_ID" in codes
    assert output["catalog"]["entries"] == []


def test_dot_temp_bundle_is_not_discovered_and_cannot_be_selected(tmp_path):
    root, catalog, active = _models_root(tmp_path)
    write_bundle(catalog / ".partial-copy.tmp", model_id="partial-model")
    with pytest.raises(ModelBackendError) as exc:
        _select(active, root, "partial-model", ".partial-copy.tmp")
    assert exc.value.code == "ACTIVE_SELECTION_INVALID"
    output, code = run(_args(root))
    assert code == 2
    assert output["error_code"] == "NO_ACTIVE_SELECTION"
    assert output["catalog"]["entries"] == []


def test_incomplete_bundle_is_not_discovered(tmp_path):
    root, catalog, active = _models_root(tmp_path)
    partial = catalog / "incomplete"
    write_bundle(partial, model_id="incomplete-model", write_checksums=False)
    _select(active, root, "incomplete-model", "incomplete")
    output, code = run(_args(root))
    assert code == 2
    assert output["error_code"] == "ACTIVE_SELECTION_INVALID"
    codes = {problem["code"] for problem in output["catalog"]["problems"]}
    assert codes == {"SHA256SUMS_INVALID"}


def test_manifest_artifact_tamper_fails_closed(tmp_path):
    root, catalog, active = _models_root(tmp_path)
    write_bundle(catalog / "tampered", model_id="tampered-model",
                 manifest_overrides={"artifact_sha256": "0" * 64})
    _select(active, root, "tampered-model", "tampered")
    output, code = run(_args(root))
    assert code == 2
    assert output["error_code"] == "ACTIVE_SELECTION_INVALID"
    codes = {problem["code"] for problem in output["catalog"]["problems"]}
    assert "ARTIFACT_HASH_MISMATCH" in codes


def test_probe_output_is_bounded(tmp_path):
    root, _catalog, _active = _models_root(tmp_path)
    output, _code = run(_args(root))
    text = json.dumps(output, ensure_ascii=False)
    assert ":\\" not in text
    assert "hidden" not in text.lower()
    assert "cookie" not in text.lower()
    assert "payload" not in text.lower()
