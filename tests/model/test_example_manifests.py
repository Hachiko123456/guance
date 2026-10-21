"""Docs examples must parse strictly and carry verifiable bundle identities.

FableDan example = complete bundle recipe (real vendor weights identity).
MARVEL example = template pinned to the Phase 6 evidence descriptor identity;
it is explicitly NOT a standalone usable bundle (no artifact ships with it).
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from guance.infrastructure.model.errors import ModelBackendError
from guance.infrastructure.model.manifest import ModelManifest, load_bundle, strict_json
from guance.infrastructure.model.registry import default_registry

ROOT = Path(__file__).parents[2]
EXAMPLES = ROOT / "docs" / "examples"
FABLEDAN_EXAMPLE = EXAMPLES / "model-bundle-fabledan-v2.json"
MARVEL_EXAMPLE = EXAMPLES / "model-bundle-marvel-v2.json"
MARVEL_DESCRIPTOR = (ROOT / "build" / "acceptance" / "guandan-marvel-v2-phase6-20261013"
                     / "experimental-bundle" / "deployment.json")
FABLEDAN_WEIGHTS = ROOT / "data" / "profiles" / "tencent_daguandan" / "models" / "best.npz"


def test_examples_are_strict_json_and_parse_as_catalog_manifests():
    for path in (FABLEDAN_EXAMPLE, MARVEL_EXAMPLE):
        data = strict_json(path.read_text(encoding="utf-8"))
        manifest = ModelManifest.from_dict(data)
        assert manifest.is_catalog_manifest is True
        assert manifest.model_id


def test_fabledan_example_is_a_complete_bundle_recipe(tmp_path):
    data = strict_json(FABLEDAN_EXAMPLE.read_text(encoding="utf-8"))
    if not FABLEDAN_WEIGHTS.is_file():
        pytest.skip("private best.npz is unavailable; the recipe identity cannot be re-verified")
    assert FABLEDAN_WEIGHTS.stat().st_size == data["artifact_bytes"]
    assert hashlib.sha256(FABLEDAN_WEIGHTS.read_bytes()).hexdigest() == data["artifact_sha256"]
    bundle = tmp_path / "fabledan-default"
    bundle.mkdir()
    shutil.copyfile(FABLEDAN_WEIGHTS, bundle / data["artifact"])
    shutil.copyfile(FABLEDAN_EXAMPLE, bundle / "manifest.json")
    (bundle / "SHA256SUMS").write_text(
        f'{data["artifact_sha256"]}  {data["artifact"]}\n', encoding="utf-8")
    loaded = load_bundle(bundle)
    assert loaded.manifest.model_id == "fabledan-default"
    assert loaded.artifact_path == bundle / data["artifact"]
    default_registry().resolve_discovery(loaded.manifest)


def test_marvel_example_is_a_documented_template_not_a_usable_bundle(tmp_path):
    data = strict_json(MARVEL_EXAMPLE.read_text(encoding="utf-8"))
    assert data["artifact_format"] == "guance_marvel_v2_deployment_v1"
    assert data["artifact_bytes"] > 0
    assert data["classification"] == "evaluation_only"
    docs = (ROOT / "docs" / "MODEL_BACKENDS.md").read_text(encoding="utf-8")
    assert "model-bundle-marvel-v2.json" in docs and "模板" in docs
    manifest_only = tmp_path / "template-only"
    manifest_only.mkdir()
    shutil.copyfile(MARVEL_EXAMPLE, manifest_only / "manifest.json")
    with pytest.raises(ModelBackendError) as exc:
        load_bundle(manifest_only)
    assert exc.value.code == "ARTIFACT_MISSING"
    if not MARVEL_DESCRIPTOR.is_file():
        pytest.skip("Phase 6 deployment evidence bundle is absent from this checkout")
    assert MARVEL_DESCRIPTOR.stat().st_size == data["artifact_bytes"]
    assert hashlib.sha256(MARVEL_DESCRIPTOR.read_bytes()).hexdigest() == data["artifact_sha256"]
    complete = tmp_path / "marvel-iter100"
    complete.mkdir()
    shutil.copyfile(MARVEL_DESCRIPTOR, complete / data["artifact"])
    shutil.copyfile(MARVEL_EXAMPLE, complete / "manifest.json")
    (complete / "SHA256SUMS").write_text(
        f'{data["artifact_sha256"]}  {data["artifact"]}\n', encoding="utf-8")
    loaded = load_bundle(complete)
    assert loaded.manifest.model_id == "marvel-v2-iter000100"
    default_registry().resolve_discovery(loaded.manifest)
