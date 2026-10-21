"""Phase 6 packaging-tool acceptance tests.

The tool is the stable training-side delivery boundary: bytes/SHA-256 are
verified first, the production provider/deployment validation must accept the
staged bundle, and only then is it atomically installed into a catalog. These
tests use real bundle content (real iter500 deployment descriptor, real FableDan
weights) but never load Torch or model weights.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from scripts.package_model_bundle import (  # noqa: E402
    BundlePackagingError, package_bundle,
)
from tests.model.model_bundle_factory import base_v2_manifest  # noqa: E402

BEST_NPZ = ROOT / "data" / "profiles" / "tencent_daguandan" / "models" / "best.npz"
GPU_BUNDLES = ROOT / "build" / "acceptance" / "guance-gpu-first-inference-20261020" / "bundles"
ITER500_BUNDLE = GPU_BUNDLES / "marvel-v2-iter000500-device"
ITER500_CHECKPOINT = Path(
    r"D:\project\guandan\runs\M09_full_training\remote"
    r"\iter500_formal_eval_20261014T030527Z\extracted\candidates\ippo_iter000500.pt")
ITER500_SHA = "f4d1d5872fa80ad1be2804ee91620046256dc0d481b78a25e4b0311f80355cea"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _catalog(tmp_path: Path) -> Path:
    catalog = tmp_path / "models" / "catalog"
    catalog.mkdir(parents=True)
    return catalog


def _fabledan_source(tmp_path: Path, model_id: str = "fabledan-phase6") -> Path:
    source = tmp_path / "source-fabledan"
    source.mkdir()
    shutil.copyfile(BEST_NPZ, source / "artifact.npz")
    manifest = base_v2_manifest(model_id, "fabledan")
    manifest.update({
        "artifact": "artifact.npz",
        "artifact_bytes": (source / "artifact.npz").stat().st_size,
        "artifact_sha256": _sha256(source / "artifact.npz"),
    })
    (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return source


def _assert_no_staging(catalog: Path) -> None:
    assert not [path for path in catalog.iterdir() if path.name.startswith(".package-")]


def test_packages_real_marvel_device_bundle_and_recomputes_checksums(tmp_path):
    catalog = _catalog(tmp_path)
    result = package_bundle(ITER500_BUNDLE, catalog, bundle_name="iter500-installed",
                            checkpoint=ITER500_CHECKPOINT)
    assert result["status"] == "installed"
    assert result["model_id"] == "marvel-v2-iter000500"
    target = catalog / "iter500-installed"
    assert target.is_dir()
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["artifact_sha256"] == _sha256(target / "deployment.json")
    sums = (target / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    entries = dict(reversed(line.split()) for line in sums)
    assert entries["deployment.json"] == _sha256(target / "deployment.json")
    assert entries["manifest.json"] == _sha256(target / "manifest.json")
    assert result["checkpoint"]["sha256"] == ITER500_SHA
    assert result["checkpoint"]["iteration"] == 500
    assert result["atomic"] is True
    _assert_no_staging(catalog)


def test_packages_fabledan_bundle_with_real_weights(tmp_path):
    catalog = _catalog(tmp_path)
    source = _fabledan_source(tmp_path)
    result = package_bundle(source, catalog, bundle_name="fabledan-installed")
    assert result["status"] == "installed"
    assert result["provider"] == "fabledan"
    assert (catalog / "fabledan-installed" / "artifact.npz").is_file()
    assert result["artifact_sha256"] == _sha256(BEST_NPZ)
    _assert_no_staging(catalog)


def test_wrong_expected_artifact_hash_is_rejected_before_staging(tmp_path):
    catalog = _catalog(tmp_path)
    with pytest.raises(BundlePackagingError) as exc:
        package_bundle(ITER500_BUNDLE, catalog, bundle_name="bad-pin",
                       expected_artifact_sha256="0" * 64)
    assert exc.value.code == "EXPECTED_HASH_MISMATCH"
    assert not (catalog / "bad-pin").exists()
    _assert_no_staging(catalog)


def test_manifest_artifact_hash_mismatch_is_rejected(tmp_path):
    catalog = _catalog(tmp_path)
    source = tmp_path / "source-tampered"
    shutil.copytree(ITER500_BUNDLE, source)
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    manifest["artifact_sha256"] = "0" * 64
    (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BundlePackagingError) as exc:
        package_bundle(source, catalog, bundle_name="tampered")
    assert exc.value.code == "ARTIFACT_HASH_MISMATCH"
    assert not (catalog / "tampered").exists()
    _assert_no_staging(catalog)


def test_existing_target_directory_is_rejected(tmp_path):
    catalog = _catalog(tmp_path)
    (catalog / "occupied").mkdir()
    with pytest.raises(BundlePackagingError) as exc:
        package_bundle(ITER500_BUNDLE, catalog, bundle_name="occupied")
    assert exc.value.code == "TARGET_EXISTS"


def test_duplicate_model_id_is_rejected(tmp_path):
    catalog = _catalog(tmp_path)
    package_bundle(ITER500_BUNDLE, catalog, bundle_name="iter500-a")
    with pytest.raises(BundlePackagingError) as exc:
        package_bundle(ITER500_BUNDLE, catalog, bundle_name="iter500-b")
    assert exc.value.code == "DUPLICATE_MODEL_ID"
    assert not (catalog / "iter500-b").exists()
    _assert_no_staging(catalog)


def test_extra_source_file_is_rejected(tmp_path):
    catalog = _catalog(tmp_path)
    source = tmp_path / "source-extra"
    shutil.copytree(ITER500_BUNDLE, source)
    (source / "notes.txt").write_text("unexpected", encoding="utf-8")
    with pytest.raises(BundlePackagingError) as exc:
        package_bundle(source, catalog, bundle_name="extra")
    assert exc.value.code == "SOURCE_BUNDLE_EXTRA_FILE"
    _assert_no_staging(catalog)


def test_checkpoint_mismatch_fails_after_staging_and_cleans_up(tmp_path):
    catalog = _catalog(tmp_path)
    checkpoint = tmp_path / "tampered.pt"
    shutil.copyfile(ITER500_CHECKPOINT, checkpoint)
    with checkpoint.open("ab") as stream:
        stream.write(b"x")
    with pytest.raises(BundlePackagingError) as exc:
        package_bundle(ITER500_BUNDLE, catalog, bundle_name="checkpoint-bad",
                       checkpoint=checkpoint)
    assert exc.value.code == "CHECKPOINT_IDENTITY_MISMATCH"
    assert not (catalog / "checkpoint-bad").exists()
    _assert_no_staging(catalog)


def test_missing_source_checksums_is_allowed_and_recomputed(tmp_path):
    catalog = _catalog(tmp_path)
    source = tmp_path / "source-no-sums"
    shutil.copytree(ITER500_BUNDLE, source)
    (source / "SHA256SUMS").unlink()
    result = package_bundle(source, catalog, bundle_name="no-sums")
    assert result["status"] == "installed"
    assert (catalog / "no-sums" / "SHA256SUMS").is_file()


def test_source_checksums_mismatch_is_rejected(tmp_path):
    catalog = _catalog(tmp_path)
    source = tmp_path / "source-bad-sums"
    shutil.copytree(ITER500_BUNDLE, source)
    (source / "SHA256SUMS").write_text("0" * 64 + "  deployment.json\n", encoding="utf-8")
    with pytest.raises(BundlePackagingError) as exc:
        package_bundle(source, catalog, bundle_name="bad-sums")
    assert exc.value.code == "SOURCE_CHECKSUMS_INVALID"
    _assert_no_staging(catalog)


def test_partial_staging_directory_is_not_discovered(tmp_path):
    catalog = _catalog(tmp_path)
    partial = catalog / ".package-deadbeef.tmp"
    shutil.copytree(ITER500_BUNDLE, partial)
    result = package_bundle(ITER500_BUNDLE, catalog, bundle_name="after-partial")
    assert result["status"] == "installed"
    assert result["catalog_entry_count"] == 1
    assert [entry["directory"] for entry in result["catalog_entries"]] == ["after-partial"]


def test_source_bundle_is_not_modified(tmp_path):
    catalog = _catalog(tmp_path)
    source = tmp_path / "source-copy"
    shutil.copytree(ITER500_BUNDLE, source)
    before = {path.name: _sha256(path) for path in source.iterdir() if path.is_file()}
    package_bundle(source, catalog, bundle_name="copy")
    after = {path.name: _sha256(path) for path in source.iterdir() if path.is_file()}
    assert before == after


def test_unsafe_bundle_name_is_rejected(tmp_path):
    catalog = _catalog(tmp_path)
    for name in ("..", ".hidden", "bad/name", "bad:name", "CON"):
        with pytest.raises(BundlePackagingError) as exc:
            package_bundle(ITER500_BUNDLE, catalog, bundle_name=name)
        assert exc.value.code == "BUNDLE_NAME_INVALID"
