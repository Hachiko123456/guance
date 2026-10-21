"""MBA-P0-001/P0-003/P0-006 and MBA-P1-001: strict bundles and explicit factories."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from guance.infrastructure.model.advisor import FableDanAdvisor
from guance.infrastructure.model.errors import ModelBackendError
from guance.infrastructure.model.factory import ModelFactory
from guance.contracts import MANIFEST_SCHEMA_VERSION
from guance.infrastructure.model.manifest import (ModelManifest, PROTOCOL_VERSION, _REQUIRED,
                                                   legacy_npz_bundle, load_bundle)
from guance.infrastructure.model.registry import ProviderRegistry, default_registry


@pytest.mark.parametrize("field", sorted(_REQUIRED))
def test_all_required_manifest_fields_enforced(manifest_data, field):
    del manifest_data[field]
    with pytest.raises(ModelBackendError, match="manifest") as exc:
        ModelManifest.from_dict(manifest_data)
    assert exc.value.code == "MANIFEST_INVALID"


@pytest.mark.parametrize("field,value,code", [
    ("protocol_version", 99, "PROTOCOL_UNSUPPORTED"),
    ("protocol_version", True, "PROTOCOL_UNSUPPORTED"),
    ("input_contract", "GameObservation.v0", "CONTRACT_UNSUPPORTED"),
    ("output_contract", "private.v1", "CONTRACT_UNSUPPORTED"),
    ("schema_version", True, "MANIFEST_INVALID"),
    ("capabilities", [], "MANIFEST_INVALID"),
    ("artifact_sha256", "BAD", "MANIFEST_INVALID"),
    ("architecture", {"private": "value"}, "MANIFEST_INVALID"),
])
def test_manifest_contract_validation(manifest_data, field, value, code):
    manifest_data[field] = value
    with pytest.raises(ModelBackendError) as exc:
        parsed = ModelManifest.from_dict(manifest_data)
        default_registry().resolve(parsed)
    assert exc.value.code == code


@pytest.mark.parametrize("path", ["../outside.npz", "/root.npz", "C:/root.npz", "C:root.npz",
    "sub/../../x", "sub\\x.npz", "sub//x.npz", "./x.npz", "NUL", "x:stream", "x.", "", "x\x00.npz"])
def test_unsafe_artifact_path_rejected(manifest_data, path):
    manifest_data["artifact"] = path
    with pytest.raises(ModelBackendError) as exc:
        ModelManifest.from_dict(manifest_data)
    assert exc.value.code == "ARTIFACT_PATH_INVALID"


def test_manifest_file_hash_and_no_fallback(manifest_path, npz_path):
    bundle = load_bundle(manifest_path.parent)
    assert not bundle.legacy
    assert bundle.artifact_path == npz_path
    npz_path.write_bytes(b"corrupt synthetic artifact")
    with pytest.raises(ModelBackendError) as exc:
        ModelFactory().create(manifest_path)
    assert exc.value.code == "ARTIFACT_HASH_MISMATCH"


def test_missing_artifact_rejected(manifest_path, npz_path):
    npz_path.unlink()
    with pytest.raises(ModelBackendError) as exc:
        load_bundle(manifest_path)
    assert exc.value.code == "ARTIFACT_MISSING"


@pytest.mark.parametrize("raw", ['[]', '{"schema_version":1,"schema_version":1}', '{"x":NaN}', '{'])
def test_malformed_manifest_json_fails_closed(manifest_path, raw):
    manifest_path.write_text(raw, encoding="utf-8")
    with pytest.raises(ModelBackendError) as exc:
        load_bundle(manifest_path)
    assert exc.value.code == "MANIFEST_INVALID"


def test_manifest_size_and_direct_descriptor_revalidation(manifest_path):
    bundle = load_bundle(manifest_path)
    with pytest.raises(ModelBackendError) as exc:
        ModelFactory().create(replace(bundle, manifest=replace(bundle.manifest, protocol_version=9)))
    assert exc.value.code == "PROTOCOL_UNSUPPORTED"
    manifest_path.write_text(" " * (64 * 1024 + 1), encoding="utf-8")
    with pytest.raises(ModelBackendError) as exc:
        load_bundle(manifest_path)
    assert exc.value.code == "MANIFEST_INVALID"


@pytest.mark.parametrize("field,value,code", [
    ("provider", "fake", "PROVIDER_UNKNOWN"), ("backend", "torch", "BACKEND_UNSUPPORTED"),
    ("runtime", "fake", "RUNTIME_UNSUPPORTED"), ("encoding_version", "new", "MODEL_VERSION_UNSUPPORTED"),
])
def test_registry_rejects_unknown_incompatible_provider_without_substitution(manifest_path, field, value, code):
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data[field] = value
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ModelBackendError) as exc:
        ModelFactory().create(manifest_path)
    assert exc.value.code == code


def test_registry_supports_explicit_test_only_family_without_global_mutation(manifest_path):
    class TestFactory:
        called = False
        def validate(self, manifest):
            assert manifest.provider == "synthetic"
        def create(self, bundle):
            self.called = True
            return "sentinel-test-advisor"
    bundle = load_bundle(manifest_path)
    bundle = replace(bundle, manifest=replace(bundle.manifest, provider="synthetic"))
    custom = TestFactory()
    registry = ProviderRegistry()
    registry.register("synthetic", custom)
    with pytest.raises(ModelBackendError) as exc:
        registry.register("synthetic", custom)
    assert exc.value.code == "PROVIDER_DUPLICATE"
    assert ModelFactory(registry).create(bundle) == "sentinel-test-advisor"
    assert custom.called
    with pytest.raises(ModelBackendError) as exc:
        default_registry().resolve(bundle.manifest)
    assert exc.value.code == "PROVIDER_UNKNOWN"


def test_legacy_npz_and_manifest_use_identical_algorithm(npz_path, manifest_path, observation, view):
    old = FableDanAdvisor(npz_path)
    expected = old.recommend(observation, "same")
    assert expected.status == "ready"
    for source in (npz_path, manifest_path):
        adapter = ModelFactory().create(source)
        metadata = adapter.load()
        assert metadata["provider"] == "fabledan"
        assert metadata["legacy"] == (source == npz_path)
        result = adapter.recommend(view, "same")
        assert [(c.cards, c.kind, c.score, c.claim) for c in result.candidates] == [
            (c.cards, c.kind, c.score, c.claim) for c in expected.candidates]
        assert result.score_kind == "fabledan_q"
        adapter.close()


def test_artifact_rechecked_at_runtime_load(manifest_path, npz_path):
    advisor = ModelFactory().create(manifest_path)
    npz_path.write_bytes(b"changed after selection")
    with pytest.raises(ModelBackendError) as exc:
        advisor.load()
    assert exc.value.code == "ARTIFACT_HASH_MISMATCH"


def test_registry_and_facade_do_not_import_optional_or_numpy_runtimes():
    result = subprocess.run([sys.executable, "-c", "import sys; "
        "from guance.infrastructure.model.registry import default_registry; default_registry(); "
        "from guance.application.model_service import ModelService; "
        "assert not any(x in sys.modules for x in ('torch','onnx','onnxruntime','numpy'))"],
        capture_output=True, text=True, timeout=10,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")})
    assert result.returncode == 0, result.stderr


def test_bundle_resolved_artifact_cannot_escape_root(manifest_path, monkeypatch):
    bundle = load_bundle(manifest_path)
    original = Path.resolve
    artifact = bundle.root / bundle.manifest.artifact
    outside = bundle.root.parent / "outside-synthetic.npz"
    def resolve(path, *args, **kwargs):
        return outside if path == artifact else original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(ModelBackendError) as exc:
        bundle.verify()
    assert exc.value.code == "ARTIFACT_PATH_INVALID"


def test_corrupt_legacy_npz_does_not_fall_back(tmp_path):
    path = tmp_path / "corrupt.npz"
    path.write_bytes(b"not-an-archive")
    advisor = ModelFactory().create(path)
    with pytest.raises(ModelBackendError) as exc:
        advisor.load()
    assert exc.value.code == "MODEL_LOAD_FAILED"


def test_public_wildcard_semantics_must_be_unique(npz_path, view):
    from guance.contracts import PublicAction
    # Heart-level wildcards with 3/4/5 admit several straight declarations.
    lead = PublicAction(1, 1, "play", (0x22, 0x122, 0x43, 0x44, 0x45), 4, 0, 0)
    follow = replace(view, free_lead=False, lead_action=lead, public_history=(lead,))
    advisor = ModelFactory().create(npz_path)
    advisor.load()
    try:
        with pytest.raises(ModelBackendError) as exc:
            advisor.recommend(follow, "request")
        assert exc.value.code == "MODEL_INPUT_AMBIGUOUS"
    finally:
        advisor.close()


def test_fabledan_load_never_imports_optional_guandan_runtimes(npz_path):
    result = subprocess.run([sys.executable, "-c", "import sys; "
        "from guance.infrastructure.model.factory import ModelFactory; "
        "advisor=ModelFactory().create(sys.argv[1]); advisor.load(); "
        "assert not any(x in sys.modules for x in ('torch','onnx','onnxruntime')); advisor.close()",
        str(npz_path)], capture_output=True, text=True, timeout=10,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")})
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("field,value,code", [
    ("backend", "unknown", "BACKEND_UNSUPPORTED"),
    ("runtime", "unknown", "RUNTIME_UNSUPPORTED"),
    ("provider", "unknown", "PROVIDER_UNKNOWN"),
])
def test_source_manifest_rejects_unknown_backend_provider_runtime(manifest_path, field, value, code):
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data[field] = value
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ModelBackendError) as exc:
        load_bundle(manifest_path)
    assert exc.value.code == code


def test_explicit_registered_provider_can_load_json_manifest_without_changing_core(manifest_path):
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data.update(provider="synthetic", backend="test-backend", runtime="test-runtime")
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    class TestFactory:
        def validate(self, manifest):
            assert manifest.provider == "synthetic"
            assert manifest.backend == "test-backend"
            assert manifest.runtime == "test-runtime"
        def create(self, bundle):
            return "explicit-test-advisor"
    registry = ProviderRegistry()
    registry.register("synthetic", TestFactory())
    assert ModelFactory(registry).create(manifest_path) == "explicit-test-advisor"
    with pytest.raises(ModelBackendError) as exc:
        load_bundle(manifest_path)
    assert exc.value.code == "PROVIDER_UNKNOWN"


def test_bool_capability_object_round_trip_without_visibility_capability(manifest_data):
    capabilities = {"recommend": True, "top_n": True, "complete_action": True,
                    "next_token_policy": False, "beam_search": False, "belief": False, "search": False}
    manifest_data["capabilities"] = capabilities
    manifest = ModelManifest.from_dict(manifest_data)
    assert dict(manifest.capabilities) == capabilities
    assert manifest.to_dict()["capabilities"] == capabilities
    capabilities["recommend"] = False
    assert manifest.capabilities["recommend"] is True
    assert "public_plus_self" not in manifest.capabilities
    with pytest.raises(TypeError):
        manifest.capabilities["recommend"] = False


@pytest.mark.parametrize("capabilities", [
    {}, {"recommend": False}, {"top_n": True}, {"recommend": 1},
    {"recommend": "true"}, {"recommend": True, "top_n": None},
    {"recommend": True, "unknown_feature": True}, {"recommend": True, "belief": {}},
])
def test_invalid_bool_capability_objects_fail_closed(manifest_data, capabilities):
    manifest_data["capabilities"] = capabilities
    with pytest.raises(ModelBackendError) as exc:
        ModelManifest.from_dict(manifest_data)
    assert exc.value.code == "MANIFEST_INVALID"


def test_legacy_list_capabilities_remain_explicitly_supported(manifest_data):
    manifest_data["capabilities"] = ["recommend", "top_n", "complete_action"]
    manifest = ModelManifest.from_dict(manifest_data)
    assert manifest.to_dict()["capabilities"] == ["recommend", "top_n", "complete_action"]


def test_canonical_alias_contracts_are_checked_by_fabledan_acl(manifest_path):
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data.update(input_contract="canonical-public-v1", output_contract="canonical-advice-v1")
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    advisor = ModelFactory().create(manifest_path)
    try:
        metadata = advisor.load()
        assert metadata["input_contract"] == "canonical-public-v1"
        assert metadata["wire_input_contract"] == "CanonicalPublicGameView.v1"
        assert metadata["output_contract"] == "canonical-advice-v1"
    finally:
        advisor.close()


def test_registered_provider_can_declare_own_contract_without_changing_manifest_parser(manifest_path):
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data.update(provider="synthetic", input_contract="synthetic-public-v8", output_contract="synthetic-advice-v8")
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    class Provider:
        def validate(self, manifest):
            assert manifest.input_contract == "synthetic-public-v8"
            assert manifest.output_contract == "synthetic-advice-v8"
        def create(self, bundle):
            return "explicit-synthetic-advisor"
    registry = ProviderRegistry()
    registry.register("synthetic", Provider())
    assert ModelFactory(registry).create(manifest_path) == "explicit-synthetic-advisor"
    assert ModelManifest.from_dict(data).input_contract == "synthetic-public-v8"
    with pytest.raises(ModelBackendError) as exc:
        load_bundle(manifest_path)
    assert exc.value.code == "PROVIDER_UNKNOWN"


def test_optional_sha256sums_present_and_valid(manifest_path, npz_path):
    digest = hashlib.sha256(npz_path.read_bytes()).hexdigest()
    (manifest_path.parent / "SHA256SUMS").write_text(
        f"{digest}  {npz_path.name}\n", encoding="utf-8")
    bundle = load_bundle(manifest_path.parent)
    assert bundle.artifact_path == npz_path


@pytest.mark.parametrize("checksum_text", [
    "0" * 64 + "  synthetic.npz\n",
    "not-a-sha256  synthetic.npz\n",
    "0" * 64 + "  ../synthetic.npz\n",
    "0" * 64 + "  C:/synthetic.npz\n",
])
def test_sha256sums_mismatch_or_invalid_entry_fails_closed(manifest_path, checksum_text):
    (manifest_path.parent / "SHA256SUMS").write_text(checksum_text, encoding="utf-8")
    with pytest.raises(ModelBackendError) as exc:
        load_bundle(manifest_path.parent)
    assert exc.value.code == "SHA256SUMS_INVALID"


def test_sha256sums_duplicate_artifact_entry_fails_closed(manifest_path, npz_path):
    digest = hashlib.sha256(npz_path.read_bytes()).hexdigest()
    (manifest_path.parent / "SHA256SUMS").write_text(
        f"{digest}  {npz_path.name}\n{digest}  {npz_path.name}\n", encoding="utf-8")
    with pytest.raises(ModelBackendError) as exc:
        load_bundle(manifest_path.parent)
    assert exc.value.code == "SHA256SUMS_INVALID"


@pytest.mark.parametrize("field,value,code", [
    ("schema_version", MANIFEST_SCHEMA_VERSION + 1, "MANIFEST_INVALID"),
    ("protocol_version", PROTOCOL_VERSION + 1, "PROTOCOL_UNSUPPORTED"),
])
def test_manifest_and_worker_protocol_versions_are_separate(manifest_data, field, value, code):
    manifest_data[field] = value
    with pytest.raises(ModelBackendError) as exc:
        ModelManifest.from_dict(manifest_data)
    assert exc.value.code == code


def test_legacy_npz_behavior_is_explicit_fabledan_compatibility(npz_path, manifest_path):
    bundle = legacy_npz_bundle(npz_path)
    assert bundle.legacy is True
    assert bundle.manifest.provider == "fabledan"
    assert bundle.manifest.artifact_format == "npz"
    with pytest.raises(ModelBackendError) as exc:
        legacy_npz_bundle(manifest_path)
    assert exc.value.code == "MODEL_SOURCE_INVALID"


def _catalog_v2_data(manifest_data: dict) -> dict:
    data = dict(manifest_data)
    data.update({
        "schema_version": 2, "model_id": "fabledan-default", "artifact_bytes": 4096,
        "runtime_contract": "fabledan.numpy.v1", "classification": "legacy",
        "readiness": {"experimental": False, "production_ready": False, "strength_claim": False},
    })
    return data


def test_catalog_v2_manifest_round_trip_and_identity(manifest_data):
    manifest = ModelManifest.from_dict(_catalog_v2_data(manifest_data))
    assert manifest.is_catalog_manifest is True
    assert manifest.model_id == "fabledan-default"
    assert manifest.artifact_bytes == 4096
    assert manifest.runtime_contract == "fabledan.numpy.v1"
    assert manifest.classification == "legacy"
    assert dict(manifest.readiness) == {"experimental": False, "production_ready": False,
                                        "strength_claim": False}
    assert ModelManifest.from_dict(manifest.to_dict()) == manifest
    assert "model_id" in manifest.to_dict()
    v1 = ModelManifest.from_dict(manifest_data)
    assert v1.is_catalog_manifest is False
    assert "model_id" not in v1.to_dict()


@pytest.mark.parametrize("mutation", [
    {"model_id": "bad id"}, {"model_id": 7}, {"model_id": ""},
    {"artifact_bytes": True}, {"artifact_bytes": 0}, {"artifact_bytes": -1},
    {"artifact_bytes": "7"}, {"artifact_bytes": 2**63},
    {"runtime_contract": "bad contract"}, {"runtime_contract": 7},
    {"classification": "top_secret"}, {"classification": 7},
    {"readiness": {}}, {"readiness": {"production_ready": True}},
    {"readiness": {"experimental": False, "production_ready": False,
                   "strength_claim": False, "extra": False}},
    {"readiness": {"experimental": 0, "production_ready": False, "strength_claim": False}},
    {"readiness": ["experimental"]},
    {"unknown_v2_key": 1},
])
def test_catalog_v2_manifest_invalid_fields_fail_closed(manifest_data, mutation):
    data = _catalog_v2_data(manifest_data)
    data.update(mutation)
    with pytest.raises(ModelBackendError) as exc:
        ModelManifest.from_dict(data)
    assert exc.value.code == "MANIFEST_INVALID"


@pytest.mark.parametrize("field", sorted({
    "model_id", "artifact_bytes", "runtime_contract", "classification", "readiness"}))
def test_catalog_v2_manifest_missing_fields_fail_closed(manifest_data, field):
    data = _catalog_v2_data(manifest_data)
    del data[field]
    with pytest.raises(ModelBackendError) as exc:
        ModelManifest.from_dict(data)
    assert exc.value.code == "MANIFEST_INVALID"


def test_v1_manifest_may_not_declare_v2_fields(manifest_data):
    data = dict(manifest_data)
    data["model_id"] = "smuggled"
    with pytest.raises(ModelBackendError) as exc:
        ModelManifest.from_dict(data)
    assert exc.value.code == "MANIFEST_INVALID"


def test_catalog_v2_artifact_bytes_enforced_at_verify(npz_path, manifest_path, manifest_data):
    import hashlib
    digest = hashlib.sha256(npz_path.read_bytes()).hexdigest()
    data = _catalog_v2_data(manifest_data)
    data["artifact_sha256"] = digest
    data["artifact_bytes"] = npz_path.stat().st_size
    manifest_path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    bundle = load_bundle(manifest_path.parent)
    assert bundle.manifest.artifact_bytes == npz_path.stat().st_size
    data["artifact_bytes"] = npz_path.stat().st_size + 1
    manifest_path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    with pytest.raises(ModelBackendError) as exc:
        load_bundle(manifest_path.parent)
    assert exc.value.code == "ARTIFACT_SIZE_MISMATCH"
