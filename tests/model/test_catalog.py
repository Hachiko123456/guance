"""P1-A1..A10/A12/N1..N9: strict catalog discovery over an explicit root."""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from guance.infrastructure.model.catalog import (
    CATALOG_DUPLICATE_MODEL_ID, ModelCatalog, scan_catalog,
)
from guance.infrastructure.model.errors import ModelBackendError
from guance.infrastructure.model.selection import safe_bundle_directory_name
from tests.model.model_bundle_factory import write_bundle


def model_ids(scan) -> list[str]:
    return [entry.model_id for entry in scan.entries]


def problem_codes(scan, directory: str) -> list[str]:
    return sorted(problem.code for problem in scan.problems if problem.directory == directory)


def test_valid_fabledan_bundle_discovered(tmp_path):
    catalog = tmp_path / "catalog"
    write_bundle(catalog / "fabledan-default", model_id="fabledan-default")
    scan = scan_catalog(catalog)
    assert scan.problems == ()
    assert len(scan.entries) == 1
    entry = scan.entries[0]
    assert (entry.model_id, entry.directory, entry.provider, entry.backend) == (
        "fabledan-default", "fabledan-default", "fabledan", "numpy")
    assert entry.runtime_contract == "fabledan.numpy.v1"
    assert entry.classification == "legacy"
    assert (entry.experimental, entry.production_ready, entry.strength_claim) == (False, False, False)
    assert entry.source == catalog / "fabledan-default"


def test_marvel_iter100_and_iter2000_discovered_simultaneously(tmp_path):
    catalog = tmp_path / "catalog"
    write_bundle(catalog / "marvel-iter100", model_id="marvel-v2-iter000100",
                 provider="guandan_marvel_v2")
    write_bundle(catalog / "marvel-iter2000", model_id="marvel-v2-iter002000",
                 provider="guandan_marvel_v2",
                 manifest_overrides={"model_version": "MARVEL-V2-v1.3-iter002000",
                                     "training_version": "M09-IPPO-two-stage-20261014"})
    scan = scan_catalog(catalog)
    assert scan.problems == ()
    assert model_ids(scan) == ["marvel-v2-iter000100", "marvel-v2-iter002000"]
    assert all(entry.provider == "guandan_marvel_v2" for entry in scan.entries)
    assert all(entry.classification == "evaluation_only" for entry in scan.entries)


def test_scan_ordering_is_deterministic_by_model_id(tmp_path):
    catalog = tmp_path / "catalog"
    write_bundle(catalog / "zzz-directory", model_id="b-model")
    write_bundle(catalog / "aaa-directory", model_id="a-model")
    scan = scan_catalog(catalog)
    assert model_ids(scan) == ["a-model", "b-model"]


def test_scan_only_direct_children_and_missing_root_is_empty(tmp_path):
    catalog = tmp_path / "catalog"
    write_bundle(catalog / "valid", model_id="valid-model")
    (catalog / "loose.pt").write_bytes(b"bare-checkpoint")
    write_bundle(catalog / "nested" / "child", model_id="nested-model")
    write_bundle(catalog / ".hidden", model_id="hidden-model")
    scan = scan_catalog(catalog)
    assert model_ids(scan) == ["valid-model"]
    absent = scan_catalog(tmp_path / "absent")
    assert absent.entries == () and absent.problems == ()
    as_file = tmp_path / "catalog-file"
    as_file.write_text("not a directory", encoding="utf-8")
    assert scan_catalog(as_file).entries == ()


def test_duplicate_model_id_excluded_and_reported(tmp_path):
    catalog = tmp_path / "catalog"
    write_bundle(catalog / "first", model_id="same-model")
    write_bundle(catalog / "second", model_id="same-model")
    write_bundle(catalog / "third", model_id="other-model")
    scan = scan_catalog(catalog)
    assert model_ids(scan) == ["other-model"]
    assert problem_codes(scan, "first") == [CATALOG_DUPLICATE_MODEL_ID]
    assert problem_codes(scan, "second") == [CATALOG_DUPLICATE_MODEL_ID]


def test_artifact_missing_hash_bytes_and_checksums_rejected(tmp_path):
    catalog = tmp_path / "catalog"
    missing = write_bundle(catalog / "missing-artifact", model_id="missing-artifact")
    (missing / "artifact.npz").unlink()
    tampered = write_bundle(catalog / "tampered", model_id="tampered")
    (tampered / "artifact.npz").write_bytes(b"y" * len(b"synthetic-artifact"))
    wrong_bytes = write_bundle(catalog / "wrong-bytes", model_id="wrong-bytes",
                               manifest_overrides={"artifact_bytes": 1})
    no_sums = write_bundle(catalog / "no-sums", model_id="no-sums", write_checksums=False)
    bad_sums = write_bundle(catalog / "bad-sums", model_id="bad-sums",
                            checksum_text="0" * 64 + "  artifact.npz\n")
    scan = scan_catalog(catalog)
    assert scan.entries == ()
    assert problem_codes(scan, "missing-artifact") == ["ARTIFACT_MISSING"]
    assert problem_codes(scan, "tampered") == ["ARTIFACT_HASH_MISMATCH"]
    assert problem_codes(scan, "wrong-bytes") == ["ARTIFACT_SIZE_MISMATCH"]
    assert problem_codes(scan, "no-sums") == ["SHA256SUMS_INVALID"]
    assert problem_codes(scan, "bad-sums") == ["SHA256SUMS_INVALID"]
    for directory in (missing, tampered, wrong_bytes, no_sums, bad_sums):
        assert directory.is_dir()


@pytest.mark.parametrize("overrides,code", [
    ({"provider": "fake"}, "PROVIDER_UNKNOWN"),
    ({"backend": "torch"}, "BACKEND_UNSUPPORTED"),
    ({"runtime": "onnx"}, "RUNTIME_UNSUPPORTED"),
    ({"runtime_contract": "fabledan.numpy.v9"}, "RUNTIME_UNSUPPORTED"),
    ({"artifact_format": "torch_checkpoint"}, "MODEL_VERSION_UNSUPPORTED"),
])
def test_unknown_or_incompatible_family_rejected(tmp_path, overrides, code):
    catalog = tmp_path / "catalog"
    write_bundle(catalog / "bundle", model_id="bundle", manifest_overrides=overrides)
    scan = scan_catalog(catalog)
    assert scan.entries == ()
    assert problem_codes(scan, "bundle") == [code]


@pytest.mark.parametrize("overrides,drops", [
    ({"classification": "top_secret"}, ()),
    ({"readiness": {"production_ready": True}}, ()),
    ({"artifact_bytes": True}, ()),
    ({"artifact_bytes": 0}, ()),
    ({"model_id": "bad id"}, ()),
    ({"model_id": 7}, ()),
    ({"runtime_contract": "../escape"}, ()),
    ({"unknown_extra": 1}, ()),
    ({"schema_version": 3}, ()),
    ({}, ("runtime_contract",)),
    ({}, ("readiness",)),
    ({}, ("artifact_bytes",)),
    ({}, ("classification",)),
    ({}, ("model_id",)),
])
def test_v2_manifest_strict_schema_rejected(tmp_path, overrides, drops):
    catalog = tmp_path / "catalog"
    write_bundle(catalog / "bundle", model_id="bundle",
                 manifest_overrides=overrides, manifest_drops=drops)
    scan = scan_catalog(catalog)
    assert scan.entries == ()
    assert problem_codes(scan, "bundle") == ["MANIFEST_INVALID"]


def test_duplicate_json_members_and_non_finite_values_rejected(tmp_path):
    catalog = tmp_path / "catalog"
    bundle = write_bundle(catalog / "dup", model_id="dup", write_manifest=False)
    (bundle / "manifest.json").write_text(
        '{"schema_version":2,"schema_version":2}', encoding="utf-8")
    write_bundle(catalog / "nan", model_id="nan", write_manifest=False)
    (catalog / "nan" / "manifest.json").write_text('{"schema_version":NaN}', encoding="utf-8")
    scan = scan_catalog(catalog)
    assert scan.entries == ()
    assert problem_codes(scan, "dup") == ["MANIFEST_INVALID"]
    assert problem_codes(scan, "nan") == ["MANIFEST_INVALID"]


@pytest.mark.parametrize("artifact", ["../outside.npz", "/root.npz", "C:/root.npz",
                                      "sub\\x.npz", "x:stream", "NUL"])
def test_artifact_path_escape_rejected(tmp_path, artifact):
    catalog = tmp_path / "catalog"
    write_bundle(catalog / "bundle", model_id="bundle",
                 manifest_overrides={"artifact": artifact})
    scan = scan_catalog(catalog)
    assert scan.entries == ()
    assert problem_codes(scan, "bundle") == ["ARTIFACT_PATH_INVALID"]


def test_missing_manifest_and_bare_pt_rejected(tmp_path):
    catalog = tmp_path / "catalog"
    (catalog / "only-pt").mkdir(parents=True)
    (catalog / "only-pt" / "model.pt").write_bytes(b"weights")
    write_bundle(catalog / "valid", model_id="valid-model")
    scan = scan_catalog(catalog)
    assert model_ids(scan) == ["valid-model"]
    assert problem_codes(scan, "only-pt") == ["MANIFEST_MISSING"]


@pytest.mark.parametrize("name", ["..", ".", ".hidden", "a/b", "a\\b", "CON", "aux",
                                  "x.", "x ", "x:y", "a\x00b", "d" * 129, ""])
def test_reserved_ads_traversal_directory_names_rejected(name):
    with pytest.raises(ModelBackendError) as exc:
        safe_bundle_directory_name(name)
    assert exc.value.code == "ARTIFACT_PATH_INVALID"


@pytest.mark.parametrize("name", ["valid", "valid-name", "valid.name", "模型包", "a b"])
def test_safe_directory_names_accepted(name):
    assert safe_bundle_directory_name(name) == name


@pytest.mark.parametrize("attribute", [0x400, stat.S_IFLNK])
def test_reparse_like_bundle_directory_rejected(tmp_path, monkeypatch, attribute):
    catalog = tmp_path / "catalog"
    bundle = write_bundle(catalog / "linked", model_id="linked")
    original = os.lstat
    def fake(path, *args, **kwargs):
        if Path(path) == bundle:
            info = original(path, *args, **kwargs)
            class Reparsed:
                st_mode = attribute if attribute == stat.S_IFLNK else info.st_mode
                st_file_attributes = 0x400
            return Reparsed()
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, "lstat", fake)
    scan = scan_catalog(catalog)
    assert scan.entries == ()
    assert problem_codes(scan, "linked") == ["ARTIFACT_PATH_INVALID"]


def test_reparse_like_catalog_root_fails_closed(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog"
    write_bundle(catalog / "valid", model_id="valid-model")
    original = os.lstat
    def fake(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if Path(path) == catalog:
            class Reparsed:
                st_mode = info.st_mode
                st_file_attributes = 0x400
            return Reparsed()
        return info
    monkeypatch.setattr(os, "lstat", fake)
    with pytest.raises(ModelBackendError) as exc:
        scan_catalog(catalog)
    assert exc.value.code == "ARTIFACT_PATH_INVALID"


def test_reparse_like_artifact_rejected(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog"
    bundle = write_bundle(catalog / "linked-arrow", model_id="linked-arrow")
    artifact = bundle / "artifact.npz"
    original = os.lstat
    def fake(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if Path(path) == artifact:
            class Reparsed:
                st_mode = info.st_mode
                st_file_attributes = 0x400
            return Reparsed()
        return info
    monkeypatch.setattr(os, "lstat", fake)
    scan = scan_catalog(catalog)
    assert scan.entries == ()
    assert problem_codes(scan, "linked-arrow") == ["ARTIFACT_PATH_INVALID"]


def test_provider_never_guessed_from_extension(tmp_path):
    catalog = tmp_path / "catalog"
    write_bundle(catalog / "renamed", model_id="renamed-model", artifact_name="artifact.bin")
    scan = scan_catalog(catalog)
    # provider comes from manifest.provider, not the artifact name
    assert model_ids(scan) == ["renamed-model"]
    assert scan.entries[0].provider == "fabledan"
    write_bundle(catalog / "unknown-provider", model_id="unknown-provider",
                 manifest_overrides={"provider": "fakedan"})
    scan = scan_catalog(catalog)
    assert problem_codes(scan, "unknown-provider") == ["PROVIDER_UNKNOWN"]


def test_scan_invalid_entry_does_not_hide_valid_entries(tmp_path):
    catalog = tmp_path / "catalog"
    write_bundle(catalog / "good", model_id="good")
    bad = write_bundle(catalog / "bad", model_id="bad", write_manifest=False)
    (bad / "manifest.json").write_text("not-json", encoding="utf-8")
    scan = scan_catalog(catalog)
    assert model_ids(scan) == ["good"]
    assert problem_codes(scan, "bad") == ["MANIFEST_INVALID"]


def test_catalog_scan_fabledan_only_imports_no_heavy_runtimes(tmp_path):
    import subprocess
    import sys
    catalog = tmp_path / "catalog"
    write_bundle(catalog / "fabledan-only", model_id="fabledan-only")
    code = (
        "import sys\n"
        "from guance.infrastructure.model.catalog import scan_catalog\n"
        "scan = scan_catalog(sys.argv[1])\n"
        "assert [e.model_id for e in scan.entries] == ['fabledan-only']\n"
        "blocked = ('torch', 'onnx', 'onnxruntime', 'guandan_marvel', 'nanopokerlib', 'numpy')\n"
        "assert not any(name in sys.modules for name in blocked), sorted(sys.modules)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")}
    completed = subprocess.run([sys.executable, "-B", "-c", code, str(catalog)],
                               capture_output=True, text=True, timeout=60, env=env)
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_resolve_bundle_exact_identity_and_fail_closed(tmp_path):
    models = tmp_path / "models"
    catalog_root = models / "catalog"
    write_bundle(catalog_root / "bundle", model_id="bundle")
    catalog = ModelCatalog(catalog_root, models / "active_model.json", root=models)
    assert catalog.resolve_bundle("bundle", "bundle") == catalog_root / "bundle"
    with pytest.raises(ModelBackendError) as exc:
        catalog.resolve_bundle("other", "bundle")
    assert exc.value.code == "ACTIVE_SELECTION_INVALID"
    with pytest.raises(ModelBackendError) as exc:
        catalog.resolve_bundle("bundle", "absent")
    assert exc.value.code == "ACTIVE_SELECTION_INVALID"
    with pytest.raises(ModelBackendError) as exc:
        catalog.resolve_bundle("bundle", "../escape")
    assert exc.value.code == "ACTIVE_SELECTION_INVALID"
    with pytest.raises(ModelBackendError) as exc:
        catalog.resolve_bundle("bundle", "CON")
    assert exc.value.code == "ACTIVE_SELECTION_INVALID"


# --- second repair: duplicate model_id must never resolve ---------------------


def _models_catalog(tmp_path):
    models = tmp_path / "models"
    catalog_root = models / "catalog"
    return models, catalog_root, ModelCatalog(catalog_root, models / "active_model.json", root=models)


def test_resolve_bundle_rejects_duplicate_model_id(tmp_path):
    models, catalog_root, catalog = _models_catalog(tmp_path)
    write_bundle(catalog_root / "a", model_id="same")
    write_bundle(catalog_root / "b", model_id="same")
    scan = scan_catalog(catalog_root)
    assert scan.entries == ()
    assert {problem.code for problem in scan.problems} == {"DUPLICATE_MODEL_ID"}
    for directory in ("a", "b"):
        with pytest.raises(ModelBackendError) as exc:
            catalog.resolve_bundle("same", directory)
        assert exc.value.code == "ACTIVE_SELECTION_INVALID"
    # Deleting one duplicate lets the exact (model_id, directory) pair resolve again.
    import shutil
    shutil.rmtree(catalog_root / "b")
    assert catalog.resolve_bundle("same", "a") == catalog_root / "a"
    with pytest.raises(ModelBackendError) as exc:
        catalog.resolve_bundle("same", "b")
    assert exc.value.code == "ACTIVE_SELECTION_INVALID"


def test_resolve_bundle_rejects_problem_directory_and_tampered_target(tmp_path):
    models, catalog_root, catalog = _models_catalog(tmp_path)
    write_bundle(catalog_root / "unique", model_id="unique")
    tampered = write_bundle(catalog_root / "tampered", model_id="tampered")
    (tampered / "artifact.npz").write_bytes(b"z" * len(b"synthetic-artifact"))
    assert catalog.resolve_bundle("unique", "unique") == catalog_root / "unique"
    with pytest.raises(ModelBackendError) as exc:
        catalog.resolve_bundle("tampered", "tampered")
    assert exc.value.code == "ACTIVE_SELECTION_INVALID"
    # Replacing the previously valid target's bytes invalidates the resolution.
    (catalog_root / "unique" / "artifact.npz").write_bytes(b"q" * len(b"synthetic-artifact"))
    with pytest.raises(ModelBackendError) as exc:
        catalog.resolve_bundle("unique", "unique")
    assert exc.value.code == "ACTIVE_SELECTION_INVALID"


def test_model_catalog_binds_catalog_root_to_trusted_root(tmp_path):
    models = tmp_path / "models"
    models.mkdir()
    outside_catalog = tmp_path / "elsewhere" / "catalog"
    with pytest.raises(ModelBackendError) as exc:
        ModelCatalog(outside_catalog, models / "active_model.json", root=models)
    assert exc.value.code == "ACTIVE_SELECTION_INVALID"
    outside_active = tmp_path / "outside-active.json"
    with pytest.raises(ModelBackendError) as exc:
        ModelCatalog(models / "catalog", outside_active, root=models)
    assert exc.value.code == "ACTIVE_SELECTION_INVALID"


# --- second repair: catalog root / parent chain reparse -----------------------


def test_real_junction_catalog_root_fails_closed(tmp_path):
    from tests.model.windows_junction import make_junction
    models = tmp_path / "models"
    real_catalog = models / "catalog-real"
    write_bundle(real_catalog / "bundle", model_id="bundle")
    make_junction(models / "catalog", real_catalog)
    with pytest.raises(ModelBackendError) as exc:
        scan_catalog(models / "catalog")
    assert exc.value.code == "ARTIFACT_PATH_INVALID"
    catalog = ModelCatalog(models / "catalog", models / "active_model.json", root=models)
    with pytest.raises(ModelBackendError) as exc:
        catalog.resolve_bundle("bundle", "bundle")
    assert exc.value.code == "ACTIVE_SELECTION_INVALID"


def test_real_junction_models_parent_chain_fails_closed(tmp_path):
    from tests.model.windows_junction import make_junction
    real_models = tmp_path / "real-models"
    write_bundle(real_models / "catalog" / "bundle", model_id="bundle")
    models = tmp_path / "models"
    make_junction(models, real_models)
    with pytest.raises(ModelBackendError) as exc:
        scan_catalog(models / "catalog")
    assert exc.value.code == "ARTIFACT_PATH_INVALID"
    catalog = ModelCatalog(models / "catalog", models / "active_model.json", root=models)
    with pytest.raises(ModelBackendError) as exc:
        catalog.resolve_bundle("bundle", "bundle")
    assert exc.value.code == "ACTIVE_SELECTION_INVALID"


def test_real_root_replaced_by_junction_after_scan_fails_closed(tmp_path):
    from tests.model.windows_junction import make_junction
    models, catalog_root, catalog = _models_catalog(tmp_path)
    write_bundle(catalog_root / "bundle", model_id="bundle")
    assert [entry.model_id for entry in catalog.scan().entries] == ["bundle"]
    moved = models / "catalog-moved"
    catalog_root.rename(moved)
    make_junction(catalog_root, moved)
    with pytest.raises(ModelBackendError) as exc:
        catalog.scan()
    assert exc.value.code == "ARTIFACT_PATH_INVALID"
    with pytest.raises(ModelBackendError) as exc:
        catalog.resolve_bundle("bundle", "bundle")
    assert exc.value.code == "ACTIVE_SELECTION_INVALID"


# --- P0 repair: provider claim authentication ---------------------------------


@pytest.mark.parametrize("overrides", [
    {"classification": "production"},
    {"classification": "production_candidate"},
    {"classification": "experimental"},
    {"classification": "legacy"},
])
def test_marvel_forged_classification_rejected(tmp_path, overrides):
    catalog = tmp_path / "catalog"
    write_bundle(catalog / "marvel", model_id="marvel-v2-forged",
                 provider="guandan_marvel_v2", manifest_overrides=overrides)
    scan = scan_catalog(catalog)
    assert scan.entries == ()
    assert problem_codes(scan, "marvel") == ["CLAIM_UNSUPPORTED"]


@pytest.mark.parametrize("readiness", [
    {"experimental": True, "production_ready": True, "strength_claim": False},
    {"experimental": True, "production_ready": False, "strength_claim": True},
    {"experimental": False, "production_ready": False, "strength_claim": False},
    {"experimental": True, "production_ready": True, "strength_claim": True},
])
def test_marvel_forged_readiness_rejected(tmp_path, readiness):
    catalog = tmp_path / "catalog"
    write_bundle(catalog / "marvel", model_id="marvel-v2-forged",
                 provider="guandan_marvel_v2", manifest_overrides={"readiness": readiness})
    scan = scan_catalog(catalog)
    assert scan.entries == ()
    assert problem_codes(scan, "marvel") == ["CLAIM_UNSUPPORTED"]


@pytest.mark.parametrize("overrides", [
    {"classification": "production"},
    {"classification": "production_candidate"},
    {"readiness": {"experimental": False, "production_ready": True, "strength_claim": False}},
    {"readiness": {"experimental": False, "production_ready": False, "strength_claim": True}},
    {"classification": "experimental",
     "readiness": {"experimental": False, "production_ready": False, "strength_claim": False}},
    {"classification": "legacy",
     "readiness": {"experimental": True, "production_ready": False, "strength_claim": False}},
])
def test_fabledan_forged_production_or_strength_claim_rejected(tmp_path, overrides):
    catalog = tmp_path / "catalog"
    write_bundle(catalog / "fabledan", model_id="fabledan-forged",
                 manifest_overrides=overrides)
    scan = scan_catalog(catalog)
    assert scan.entries == ()
    assert problem_codes(scan, "fabledan") == ["CLAIM_UNSUPPORTED"]


def test_fabledan_evaluation_only_variant_discovered(tmp_path):
    catalog = tmp_path / "catalog"
    write_bundle(catalog / "fabledan-eval", model_id="fabledan-eval",
                 manifest_overrides={
                     "classification": "evaluation_only",
                     "readiness": {"experimental": True, "production_ready": False,
                                   "strength_claim": False}})
    scan = scan_catalog(catalog)
    assert model_ids(scan) == ["fabledan-eval"]
    assert scan.entries[0].classification == "evaluation_only"
    assert scan.entries[0].production_ready is False


def test_guandan_scaffold_discovered_and_claims_bounded(tmp_path):
    catalog = tmp_path / "catalog"
    write_bundle(catalog / "guandan-ok", model_id="guandan-ok", provider="guandan")
    scan = scan_catalog(catalog)
    assert model_ids(scan) == ["guandan-ok"]
    assert scan.entries[0].provider == "guandan"
    write_bundle(catalog / "guandan-forged", model_id="guandan-forged", provider="guandan",
                 manifest_overrides={"classification": "production"})
    write_bundle(catalog / "guandan-forged2", model_id="guandan-forged2", provider="guandan",
                 manifest_overrides={"readiness": {"experimental": True,
                                                   "production_ready": True,
                                                   "strength_claim": False}})
    scan = scan_catalog(catalog)
    assert problem_codes(scan, "guandan-forged") == ["CLAIM_UNSUPPORTED"]
    assert problem_codes(scan, "guandan-forged2") == ["CLAIM_UNSUPPORTED"]


# --- P0 repair: real Windows reparse boundaries -------------------------------


def test_real_junction_bundle_directory_rejected(tmp_path):
    from tests.model.windows_junction import make_junction
    real_bundle = write_bundle(tmp_path / "real-bundle", model_id="junctional")
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    make_junction(catalog / "linked-bundle", real_bundle)
    scan = scan_catalog(catalog)
    assert scan.entries == ()
    assert problem_codes(scan, "linked-bundle") == ["ARTIFACT_PATH_INVALID"]


def test_real_junction_sha256sums_rejected(tmp_path):
    from tests.model.windows_junction import make_junction
    bundle = write_bundle(tmp_path / "catalog" / "bundle", model_id="bundle",
                          write_checksums=False)
    target_directory = tmp_path / "sums-target"
    target_directory.mkdir()
    make_junction(bundle / "SHA256SUMS", target_directory)
    scan = scan_catalog(tmp_path / "catalog")
    assert scan.entries == ()
    assert problem_codes(scan, "bundle") == ["SHA256SUMS_INVALID"]


def test_real_file_symlink_sha256sums_rejected(tmp_path):
    from tests.model.windows_junction import make_file_symlink
    bundle = write_bundle(tmp_path / "catalog" / "bundle", model_id="bundle",
                          write_checksums=False)
    other = tmp_path / "other-sums"
    other.write_text("", encoding="utf-8")
    make_file_symlink(bundle / "SHA256SUMS", other)
    scan = scan_catalog(tmp_path / "catalog")
    assert scan.entries == ()
    assert problem_codes(scan, "bundle") == ["SHA256SUMS_INVALID"]
