"""Phase 2 provider/worker integration on the frozen materialized runtime.

The default runtime/bundle materials live under the Phase 2 evidence directory
(rebuildable with scripts/build_marvel_materials.py). The Guandan worktree is
never used as a runtime root. Recommendation stays unavailable; no fallback.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.abc
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from guance.contracts import CanonicalPublicGameView, ModelMetadata
from guance.infrastructure.model.client import InferenceClient
from guance.infrastructure.model.errors import ModelBackendError
from guance.infrastructure.model.factory import ModelFactory
from guance.infrastructure.model.manifest import load_bundle
from guance.infrastructure.model.marvel_v2.deployment_bundle import (
    CHECKPOINT_ENV, SCHEMA, SOURCE_ENV, load_descriptor,
)

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = Path(r"D:\project\guance\build\acceptance\guance-model-platform-20261016\phase2")
RUNTIME = EVIDENCE / "runtime" / "marvel-e5c82e7c-d7acbc8d"
RUNTIME_SOURCE = RUNTIME / "source"
BUNDLES = {
    "iter100": EVIDENCE / "bundles" / "marvel-v2-iter000100",
    "iter2000": EVIDENCE / "bundles" / "marvel-v2-iter002000",
}
CHECKPOINTS = {
    "iter100": Path(r"D:\project\guandan\runs\comparison_delivery\dataset\m09_champion_iter000100.pt"),
    "iter2000": Path(r"D:\project\guandan\runs\comparison_delivery\dataset\m09_candidate_iter002000.pt"),
}


def _cuda_build() -> bool:
    try:
        import torch
    except Exception:
        return False
    return bool(torch.version.cuda)


CPU_PINNED = pytest.mark.skipif(
    _cuda_build(), reason="Phase 2 descriptor pins the accepted CPU-only Torch build")


def _runtime_json() -> dict:
    path = RUNTIME / "runtime.json"
    if not path.is_file():
        pytest.skip("materialized runtime unavailable; run scripts/build_marvel_materials.py")
    return json.loads(path.read_text(encoding="utf-8"))


def _require_bundle(model: str) -> Path:
    runtime = _runtime_json()
    manifest = BUNDLES[model] / "manifest.json"
    if not manifest.is_file():
        pytest.skip("phase 2 bundle unavailable")
    if not CHECKPOINTS[model].is_file():
        pytest.skip("fixed checkpoint unavailable")
    assert runtime["four_condition_probe"]["source_matches_loaded_binary"] is True
    return manifest


def _configure(monkeypatch: pytest.MonkeyPatch, model: str) -> None:
    _require_bundle(model)
    monkeypatch.setenv(SOURCE_ENV, str(RUNTIME_SOURCE))
    monkeypatch.setenv(CHECKPOINT_ENV, str(CHECKPOINTS[model]))


@pytest.mark.parametrize("model", ["iter100", "iter2000"])
def test_phase2_descriptor_is_exact_bounded_and_not_a_checkpoint_copy(model: str) -> None:
    runtime = _runtime_json()
    manifest_path = _require_bundle(model)
    bundle = load_bundle(manifest_path)
    descriptor = load_descriptor(bundle)
    wire = json.loads(bundle.artifact_path.read_text(encoding="utf-8"))
    checkpoint = CHECKPOINTS[model]
    assert wire["schema"] == SCHEMA
    assert descriptor.checkpoint["sha256"] == hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    assert descriptor.checkpoint["bytes"] == checkpoint.stat().st_size
    assert descriptor.runtime["identity_sha256"] == runtime["deployment"]["identity_sha256"]
    assert descriptor.runtime["native_sha256"] == runtime["native"]["sha256"]
    assert descriptor.source_fingerprint["hash"] == runtime["source_fingerprint"]["hash"]
    assert descriptor.readiness == {
        "experimental": True, "test_only": False, "production_ready": False,
        "public_self_supported": False, "live_eligible_set": (),
    }
    assert descriptor.checkpoint_expectation.payload_variant in {
        "ddp_merged_frozen_v1", "flat_not_smoothed_v1"}
    assert bundle.artifact_path.stat().st_size < 64 * 1024
    assert bundle.artifact_path != checkpoint
    assert not any(path.suffix == ".pt" for path in manifest_path.parent.iterdir())


@pytest.mark.parametrize("section,key,value", [
    ("checkpoint", "sha256", "0" * 64),
    ("checkpoint", "classification", "training"),
    ("checkpoint", "resume_classification", "training"),
    ("checkpoint", "payload_variant", "future_unknown_v2"),
    ("checkpoint", "source_parent_commit", "0" * 40),
    ("runtime", "identity_sha256", "0" * 64),
    ("runtime", "native_sha256", "0" * 64),
    ("runtime", "torch_version", "unknown"),
    ("evaluation", "formal_status", "passed"),
    ("evaluation", "strength_claim", True),
    ("readiness", "production_ready", True),
    ("readiness", "public_self_supported", True),
    ("readiness", "live_eligible_set", ["guessed"]),
])
def test_phase2_descriptor_identity_mismatch_fails_closed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, section: str, key: str,
        value: object) -> None:
    source = _require_bundle("iter100")
    bundle_dir = tmp_path / "marvel-v2"
    bundle_dir.mkdir()
    shutil.copyfile(source, bundle_dir / "manifest.json")
    deployment = bundle_dir / "deployment.json"
    data = json.loads((source.parent / "deployment.json").read_text(encoding="utf-8"))
    data[section][key] = value
    deployment.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
    digest = hashlib.sha256(deployment.read_bytes()).hexdigest()
    manifest_data = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest_data["artifact_sha256"] = digest
    manifest_data["artifact_bytes"] = deployment.stat().st_size
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest_data, sort_keys=True) + "\n",
                                              encoding="utf-8")
    (bundle_dir / "SHA256SUMS").write_text(f"{digest}  deployment.json\n", encoding="utf-8")
    _configure(monkeypatch, "iter100")
    with pytest.raises(ModelBackendError):
        ModelFactory().create(bundle_dir / "manifest.json").load()


@CPU_PINNED
@pytest.mark.parametrize("model,iteration,model_version", [
    ("iter100", 100, "MARVEL-V2-v1.3-iter000100"),
    ("iter2000", 2000, "MARVEL-V2-v1.3-iter002000"),
])
def test_phase2_provider_loads_real_runtime_but_live_advice_is_unavailable(
        model: str, iteration: int, model_version: str,
        monkeypatch: pytest.MonkeyPatch, view: CanonicalPublicGameView) -> None:
    manifest_path = _require_bundle(model)
    runtime = _runtime_json()
    _configure(monkeypatch, model)
    advisor = ModelFactory().create(manifest_path)
    checkpoint_sha = hashlib.sha256(CHECKPOINTS[model].read_bytes()).hexdigest()
    try:
        metadata = advisor.load()
        assert isinstance(metadata, ModelMetadata)
        assert metadata.status == "unavailable"
        assert metadata.provider == "guandan_marvel_v2"
        assert metadata.backend == "guandan"
        assert metadata.runtime == "torch-cpu" and metadata.runtime_version == "2.13.0+cpu"
        assert metadata.model_sha256 == checkpoint_sha
        assert metadata.model_version == model_version
        assert metadata.deployment_fingerprint == runtime["deployment"]["identity_sha256"]
        assert metadata.experimental is True and metadata.test_only is False
        assert metadata.production_ready is False and metadata.strength_claim is False
        assert metadata.public_self_supported is False
        assert metadata.classification == "evaluation_only"
        assert advisor.runtime._runtime.identity.iteration == iteration
        assert advisor.runtime._runtime.identity.checkpoint_sha256 == checkpoint_sha
        assert advisor.runtime._runtime.identity.deployment_identity_verified is True
        with pytest.raises(ModelBackendError) as exc:
            advisor.recommend(view, "phase2-unavailable")
        assert exc.value.code == "GUANDAN_STATE_UNAVAILABLE"
        assert advisor.load().bundle_fingerprint == metadata.bundle_fingerprint
    finally:
        advisor.close()
        advisor.close()
    with pytest.raises(ModelBackendError) as exc:
        advisor.load()
    assert exc.value.code == "MODEL_CLOSED"


@CPU_PINNED
def test_phase2_worker_client_reports_unavailable_metadata_and_no_result(
        monkeypatch: pytest.MonkeyPatch) -> None:
    manifest_path = _require_bundle("iter2000")
    _configure(monkeypatch, "iter2000")
    statuses: list[dict] = []
    results = []
    finished = threading.Event()

    def on_status(status) -> None:
        item = status.to_dict()
        statuses.append(item)
        if item.get("model_status") in {"unavailable", "ready", "error", "timeout"}:
            finished.set()

    client = InferenceClient(manifest_path, on_result=results.append, on_status=on_status,
                             timeout_seconds=2.0, threads=1)
    try:
        client.start()
        assert finished.wait(60), statuses
        final = statuses[-1]
        assert final["model_status"] == "unavailable"
        assert final["error_code"] == "GUANDAN_STATE_UNAVAILABLE"
        assert final["metadata"]["provider"] == "guandan_marvel_v2"
        assert final["metadata"]["status"] == "unavailable"
        assert final["metadata"]["experimental"] is True
        assert final["metadata"]["production_ready"] is False
        assert results == []
    finally:
        client.close()


def test_phase2_missing_explicit_environment_never_scans_or_falls_back(
        monkeypatch: pytest.MonkeyPatch) -> None:
    manifest_path = _require_bundle("iter100")
    monkeypatch.delenv(SOURCE_ENV, raising=False)
    monkeypatch.delenv(CHECKPOINT_ENV, raising=False)
    advisor = ModelFactory().create(manifest_path)
    with pytest.raises(ModelBackendError) as exc:
        advisor.load()
    assert exc.value.code == "MODEL_UNAVAILABLE"
    advisor.close()


def test_phase2_worktree_is_not_a_trusted_runtime_root(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The live Guandan worktree must fail the descriptor fingerprint (no fallback)."""
    manifest_path = _require_bundle("iter100")
    worktree = Path(r"D:\project\guandan")
    if not worktree.is_dir():
        pytest.skip("worktree unavailable")
    monkeypatch.setenv(SOURCE_ENV, str(worktree))
    monkeypatch.setenv(CHECKPOINT_ENV, str(CHECKPOINTS["iter100"]))
    advisor = ModelFactory().create(manifest_path)
    with pytest.raises(ModelBackendError) as exc:
        advisor.load()
    assert exc.value.code in {"MODEL_VERSION_UNSUPPORTED", "MODEL_UNAVAILABLE"}
    advisor.close()


def test_phase2_default_registry_is_lazy_and_has_no_fake_provider() -> None:
    script = r'''
import importlib.abc, sys
blocked = {'torch', 'guandan_marvel', 'nanopokerlib'}
attempts = []
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in blocked:
            attempts.append(fullname)
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, Block())
from guance.infrastructure.model.registry import default_registry
registry = default_registry()
assert set(registry._providers) == {'fabledan', 'guandan', 'builtin.guandan', 'guandan_marvel_v2'}
assert not any(name in sys.modules for name in ('torch', 'guandan_marvel', 'nanopokerlib'))
assert 'guance.infrastructure.model.marvel_v2.runtime' not in sys.modules
assert 'guance.infrastructure.model.marvel_v2.deployment_bundle' not in sys.modules
assert not attempts
'''
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run([sys.executable, "-B", "-c", script], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr or result.stdout


def test_phase2_manifest_cannot_silently_switch_provider(tmp_path: Path) -> None:
    source = _require_bundle("iter100")
    bundle_dir = tmp_path / "marvel-v2"
    bundle_dir.mkdir()
    shutil.copyfile(source, bundle_dir / "manifest.json")
    shutil.copyfile(source.parent / "deployment.json", bundle_dir / "deployment.json")
    shutil.copyfile(source.parent / "SHA256SUMS", bundle_dir / "SHA256SUMS")
    data = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    data["provider"] = "fabledan"
    (bundle_dir / "manifest.json").write_text(json.dumps(data, sort_keys=True) + "\n",
                                              encoding="utf-8")
    with pytest.raises(ModelBackendError) as exc:
        ModelFactory().create(bundle_dir / "manifest.json")
    assert exc.value.code in {"BACKEND_UNSUPPORTED", "MODEL_VERSION_UNSUPPORTED"}


@CPU_PINNED
def test_secure_package_bootstrap_never_adds_source_root_to_sys_path(
        monkeypatch: pytest.MonkeyPatch, view: CanonicalPublicGameView) -> None:
    manifest_path = _require_bundle("iter100")
    _configure(monkeypatch, "iter100")
    resolved = str(RUNTIME_SOURCE.resolve())
    before_packages = {name for name in sys.modules if name == "guandan_marvel"
                       or name.startswith("guandan_marvel.") or name == "nanopokerlib"
                       or name.startswith("nanopokerlib.")}
    assert resolved not in sys.path
    advisor = ModelFactory().create(manifest_path)
    try:
        metadata = advisor.load()
        assert metadata.status == "unavailable"
        assert resolved not in sys.path
        assert Path(sys.modules["guandan_marvel"].__file__).resolve() == (
            RUNTIME_SOURCE / "guandan_marvel/__init__.py").resolve()
        assert Path(sys.modules["guandan_marvel.model"].__file__).resolve() == (
            RUNTIME_SOURCE / "guandan_marvel/model/__init__.py").resolve()
        # Deny-by-default: names outside the validated fingerprint never resolve.
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("guandan_marvel.not_a_real_module")
        with pytest.raises(ModelBackendError, match="Guandan environment state"):
            advisor.recommend(view, "secure-bootstrap")
    finally:
        advisor.close()
    assert resolved not in sys.path
    after_packages = {name for name in sys.modules if name == "guandan_marvel"
                      or name.startswith("guandan_marvel.") or name == "nanopokerlib"
                      or name.startswith("nanopokerlib.")}
    assert after_packages == before_packages


def test_unverified_source_customizers_and_shadow_modules_never_execute_before_validation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest_path = _require_bundle("iter100")
    malicious = tmp_path / "malicious-source"
    malicious.mkdir()
    sentinel = tmp_path / "bootstrap-sentinel.txt"
    payload = f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('EXECUTED', encoding='utf-8')\n"
    for name in ("sitecustomize.py", "usercustomize.py", "guance.py", "torch.py", "json.py"):
        (malicious / name).write_text(payload, encoding="utf-8")
    monkeypatch.setenv(SOURCE_ENV, str(malicious))
    monkeypatch.setenv(CHECKPOINT_ENV, str(CHECKPOINTS["iter100"]))
    monkeypatch.setenv("PYTHONPATH", str(malicious))
    statuses = []
    finished = threading.Event()
    def on_status(status) -> None:
        item = status.to_dict(); statuses.append(item)
        if item.get("model_status") in {"unavailable", "error", "timeout", "ready"}:
            finished.set()
    client = InferenceClient(manifest_path, on_result=lambda result: pytest.fail("unexpected result"),
                             on_status=on_status, timeout_seconds=2.0, threads=1)
    try:
        client.start()
        assert finished.wait(60), statuses
        assert statuses[-1]["model_status"] in {"unavailable", "error"}
        assert not any(item.get("model_status") == "ready" for item in statuses)
        assert not sentinel.exists()
    finally:
        client.close()
    assert not sentinel.exists()


def test_verified_python_loader_rechecks_bytes_before_execution(tmp_path: Path) -> None:
    from guance.infrastructure.model.marvel_v2.deployment_bundle import _VerifiedPythonLoader
    path = tmp_path / "verified_module.py"
    sentinel = tmp_path / "loader-sentinel.txt"
    safe = b"VALUE = 1\n"
    path.write_bytes(safe)
    loader = _VerifiedPythonLoader(path, len(safe), hashlib.sha256(safe).hexdigest())
    path.write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('EXECUTED', encoding='utf-8')\n",
        encoding="utf-8",
    )
    spec = importlib.util.spec_from_file_location("verified_module", path, loader=loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    with pytest.raises(ModelBackendError) as exc:
        loader.exec_module(module)
    assert exc.value.code == "MODEL_VERSION_UNSUPPORTED"
    assert not sentinel.exists()


def _copy_fingerprinted_source(destination: Path) -> None:
    fingerprint = _runtime_json()["source_fingerprint"]
    copied = set()
    for component in fingerprint["components"].values():
        for entry in component["files"]:
            relative = Path(*str(entry["path"]).split("/"))
            if relative in copied:
                continue
            copied.add(relative)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(RUNTIME_SOURCE / relative, target)


@pytest.mark.parametrize("kind", [
    "sitecustomize", "usercustomize", "torch_shadow", "guance_package", "native_python_shadow",
])
def test_complete_fingerprinted_source_rejects_forbidden_startup_and_shadow_entries(
        tmp_path: Path, kind: str) -> None:
    from guance.infrastructure.model.marvel_v2.deployment_bundle import _verify_source_tree
    runtime = _runtime_json()
    source = tmp_path / "source"
    _copy_fingerprinted_source(source)
    if kind == "sitecustomize":
        (source / "sitecustomize.py").write_text("raise AssertionError('must not execute')\n", encoding="utf-8")
    elif kind == "usercustomize":
        (source / "usercustomize.py").write_text("raise AssertionError('must not execute')\n", encoding="utf-8")
    elif kind == "torch_shadow":
        (source / "torch.py").write_text("raise AssertionError('must not execute')\n", encoding="utf-8")
    elif kind == "guance_package":
        (source / "guance").mkdir()
        (source / "guance/__init__.py").write_text("raise AssertionError('must not execute')\n", encoding="utf-8")
    else:
        (source / "nanopokerlib/_guandan_cpp.py").write_text(
            "raise AssertionError('must not execute')\n", encoding="utf-8")
    with pytest.raises(ModelBackendError) as exc:
        _verify_source_tree(source, runtime["source_fingerprint"])
    assert exc.value.code == "MODEL_VERSION_UNSUPPORTED"


@pytest.mark.parametrize("value", [
    "../escape.py", "folder/../../escape.py", "folder/file.py:ads", "CON/file.py",
    "folder/NUL.py", "folder/bad. /file.py", "folder/\x01control.py", "folder\\file.py",
])
def test_secure_relative_source_paths_reject_traversal_ads_reserved_and_control(value: str) -> None:
    from guance.infrastructure.model.marvel_v2.deployment_bundle import _safe_relative
    with pytest.raises(ModelBackendError) as exc:
        _safe_relative(value)
    assert exc.value.code == "ARTIFACT_PATH_INVALID"


def test_external_path_rejects_ntfs_alternate_data_stream(tmp_path: Path) -> None:
    from guance.infrastructure.model.marvel_v2.deployment_bundle import _strict_external_path
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"safe")
    with pytest.raises(ModelBackendError) as exc:
        _strict_external_path(str(artifact) + ":payload", directory=False)
    assert exc.value.code == "ARTIFACT_PATH_INVALID"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
def test_external_source_rejects_real_windows_junction(tmp_path: Path) -> None:
    from guance.infrastructure.model.marvel_v2.deployment_bundle import _strict_external_path
    target = tmp_path / "target"
    junction = tmp_path / "junction"
    target.mkdir()
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(target)],
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        pytest.skip("junction creation unavailable")
    try:
        with pytest.raises(ModelBackendError) as exc:
            _strict_external_path(str(junction), directory=True)
        assert exc.value.code == "ARTIFACT_PATH_INVALID"
    finally:
        os.rmdir(junction)


def test_reparse_attribute_is_rejected_even_without_symlink_api(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace
    from guance.infrastructure.model.marvel_v2.deployment_bundle import _reject_reparse_chain
    path = tmp_path / "source"
    path.mkdir()
    monkeypatch.setattr(os, "lstat", lambda candidate: SimpleNamespace(st_file_attributes=0x400))
    with pytest.raises(ModelBackendError) as exc:
        _reject_reparse_chain(path)
    assert exc.value.code == "ARTIFACT_PATH_INVALID"


@pytest.mark.skipif(os.name != "nt", reason="Windows native loader lock")
def test_locked_native_artifact_blocks_write_and_swap_until_closed(tmp_path: Path) -> None:
    from guance.infrastructure.model.marvel_v2.deployment_bundle import _lock_native_artifact
    native = tmp_path / "native.pyd"
    replacement = tmp_path / "replacement.pyd"
    native.write_bytes(b"verified-native-bytes")
    replacement.write_bytes(b"replacement")
    locked = _lock_native_artifact(native)
    try:
        assert locked.sha256 == hashlib.sha256(b"verified-native-bytes").hexdigest()
        assert locked.bytes == len(b"verified-native-bytes")
        with pytest.raises(OSError):
            native.write_bytes(b"swapped")
        with pytest.raises(OSError):
            os.replace(replacement, native)
    finally:
        locked.close()
    os.replace(replacement, native)
    assert native.read_bytes() == b"replacement"
