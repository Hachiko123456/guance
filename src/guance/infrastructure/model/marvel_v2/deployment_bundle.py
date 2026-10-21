"""Strict data-driven deployment descriptor for MARVEL V2 runtime materials.

The selected Guance bundle contains only this bounded JSON descriptor. The
descriptor declares (a) the runtime source fingerprint of the materialized
runtime installation, (b) the native extension bytes/hash/embedded source
identity, (c) the per-bundle checkpoint expectations and payload variant, and
(d) evaluation/readiness. No global checkpoint/iteration/source constants are
used for loading; the descriptor and the runtime installation are validated
against each other and against the actual environment.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib
import importlib.abc
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import platform
import stat
import struct
import sys
from types import MappingProxyType
from typing import Any, Mapping

from ..errors import ModelBackendError
from ..manifest import MAX_MANIFEST_BYTES, ModelBundle, ModelManifest, strict_json
from .checkpoint import CheckpointExpectation, canonical_json_hash
from .deployment import DeploymentIdentity, require_platform_distinct_from_training
from .identity import (
    CHECKPOINT_SCHEMA, DEPLOYMENT_ARTIFACT_FORMAT, DEPLOYMENT_SCHEMA, PROTOCOL,
    checkpoint_manifest, validate_provider_manifest,
)

SCHEMA = DEPLOYMENT_SCHEMA
SOURCE_ENV = "GUANCE_MARVEL_V2_SOURCE"
CHECKPOINT_ENV = "GUANCE_MARVEL_V2_CHECKPOINT"
_CHECKPOINT_KEYS = frozenset({
    "filename", "bytes", "sha256", "schema", "algorithm", "architecture",
    "iteration", "protocol_version", "classification", "resume_classification",
    "training_resume_allowed", "resolved_config_hash", "source_hash",
    "source_parent_commit", "training_backend_identity_hash", "payload_variant",
})
_RUNTIME_KEYS = frozenset({
    "os_name", "python_implementation", "python_version", "python_cache_tag",
    "pointer_bits", "torch_version", "torch_cuda", "native_filename", "native_sha256",
    "native_bytes", "native_source_sha256", "compiler", "cxx_standard", "backend",
    "protocol_version", "identity_sha256",
})
_EVALUATION = {"formal_status": "not_run", "champion_status": "not_established",
               "strength_claim": False}
_READINESS = {"experimental": True, "test_only": False, "production_ready": False,
              "public_self_supported": False, "live_eligible_set": []}
_TOP_KEYS = frozenset({
    "schema", "checkpoint", "source_fingerprint", "runtime", "evaluation",
    "readiness", "source_environment", "checkpoint_environment",
})
_FORBIDDEN_SOURCE_ENTRIES = frozenset({
    "sitecustomize.py", "usercustomize.py", "guance.py", "torch.py", "numpy.py",
    "json.py", "pathlib.py", "hashlib.py", "importlib.py", "os.py", "sys.py",
    "platform.py", "struct.py", "ctypes.py", "msvcrt.py",
})
_FORBIDDEN_SOURCE_PACKAGES = frozenset({"guance", "torch", "numpy"})
_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_HEX64 = set("0123456789abcdef")


def _fail(code: str = "MODEL_VERSION_UNSUPPORTED") -> None:
    raise ModelBackendError(code)


def _sha256_file(path: Path) -> tuple[str, int]:
    try:
        h = hashlib.sha256(); size = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                size += len(chunk); h.update(chunk)
        return h.hexdigest(), size
    except OSError:
        raise ModelBackendError("ARTIFACT_MISSING") from None


def _is_hex64(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX64


def _bounded_text(value: Any, *, maximum: int) -> str:
    if (not isinstance(value, str) or not value or len(value) > maximum
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        _fail("MANIFEST_INVALID")
    return value


def _safe_relative(value: Any) -> str:
    if (not isinstance(value, str) or not value or len(value) > 512
            or "\\" in value or any(ord(char) < 32 for char in value)
            or any(char in value for char in '<>:"|?*')):
        raise ModelBackendError("ARTIFACT_PATH_INVALID")
    path = PurePosixPath(value)
    parts = value.split("/")
    if (path.is_absolute() or any(
            part in ("", ".", "..") or part.endswith((" ", "."))
            or PureWindowsPath(part).is_reserved() for part in parts)):
        raise ModelBackendError("ARTIFACT_PATH_INVALID")
    return str(path)


def _strict_external_path(value: str, *, directory: bool) -> Path:
    if (not isinstance(value, str) or not value.strip() or len(value) > 4096
            or any(ord(char) < 32 for char in value)
            or any(char in value for char in '<>"|?*')):
        raise ModelBackendError("ARTIFACT_PATH_INVALID")
    raw = value.strip()
    path = Path(raw)
    if not path.is_absolute():
        raise ModelBackendError("ARTIFACT_PATH_INVALID")
    drive = path.drive
    remainder = raw[len(drive):] if drive else raw
    if ":" in remainder:  # Alternate data stream or malformed drive syntax.
        raise ModelBackendError("ARTIFACT_PATH_INVALID")
    for part in path.parts:
        if part in {path.anchor, drive, "\\", "/"}:
            continue
        if (not part or part.endswith((" ", ".")) or PureWindowsPath(part).is_reserved()
                or any(ord(char) < 32 for char in part)):
            raise ModelBackendError("ARTIFACT_PATH_INVALID")
    lexical = Path(os.path.abspath(raw))
    _reject_reparse_chain(lexical)
    if directory and not lexical.is_dir():
        raise ModelBackendError("MODEL_UNAVAILABLE")
    if not directory and not lexical.is_file():
        raise ModelBackendError("ARTIFACT_MISSING")
    return lexical.resolve()


def _reject_reparse_chain(path: Path) -> None:
    chain = [path, *path.parents]
    for candidate in reversed(chain):
        if not candidate.exists():
            continue
        try:
            info = os.lstat(candidate)
        except OSError:
            raise ModelBackendError("MODEL_UNAVAILABLE") from None
        if candidate.is_symlink() or int(getattr(info, "st_file_attributes", 0)) & _REPARSE_ATTRIBUTE:
            raise ModelBackendError("ARTIFACT_PATH_INVALID")


def _reject_forbidden_source_entries(root: Path) -> None:
    for name in _FORBIDDEN_SOURCE_ENTRIES:
        if (root / name).exists():
            raise ModelBackendError("MODEL_VERSION_UNSUPPORTED")
    for name in _FORBIDDEN_SOURCE_PACKAGES:
        if (root / name).is_dir():
            raise ModelBackendError("MODEL_VERSION_UNSUPPORTED")
    nanopokerlib = root / "nanopokerlib"
    if ((nanopokerlib / "_guandan_cpp.py").exists()
            or (nanopokerlib / "_guandan_cpp").exists()):
        raise ModelBackendError("MODEL_VERSION_UNSUPPORTED")


def _validate_source_fingerprint(value: Any) -> Mapping[str, Any]:
    if (type(value) is not dict or value.keys() != {"schema", "components", "hash"}
            or value.get("schema") != "guandan-marvel-source-fingerprint-v1"
            or not _is_hex64(value.get("hash"))
            or canonical_json_hash({"schema": value["schema"], "components": value["components"]})
            != value["hash"]):
        _fail()
    components = value["components"]
    if type(components) is not dict or not components:
        _fail()
    seen: set[str] = set()
    for component in components.values():
        if type(component) is not dict or component.keys() != {"files", "hash"}:
            _fail()
        files = component["files"]
        if type(files) is not list or not files or canonical_json_hash(files) != component["hash"]:
            _fail()
        for entry in files:
            if (type(entry) is not dict or entry.keys() != {"path", "sha256", "bytes"}
                    or _safe_relative(entry.get("path")) in seen
                    or not _is_hex64(entry.get("sha256"))
                    or type(entry.get("bytes")) is not int or entry["bytes"] < 0):
                _fail()
            seen.add(entry["path"])
    return MappingProxyType(value)


def _validate_checkpoint_block(checkpoint: Any, manifest: ModelManifest) -> CheckpointExpectation:
    if type(checkpoint) is not dict or checkpoint.keys() != _CHECKPOINT_KEYS:
        _fail("MANIFEST_INVALID")
    expectation = CheckpointExpectation(
        filename=checkpoint["filename"], bytes=checkpoint["bytes"], sha256=checkpoint["sha256"],
        schema=checkpoint["schema"], algorithm=checkpoint["algorithm"],
        architecture=checkpoint["architecture"], protocol_version=checkpoint["protocol_version"],
        iteration=checkpoint["iteration"], classification=checkpoint["classification"],
        resume_classification=checkpoint["resume_classification"],
        training_resume_allowed=checkpoint["training_resume_allowed"],
        resolved_config_hash=checkpoint["resolved_config_hash"],
        source_hash=checkpoint["source_hash"],
        source_parent_commit=checkpoint["source_parent_commit"],
        training_backend_identity_hash=checkpoint["training_backend_identity_hash"],
        payload_variant=checkpoint["payload_variant"],
        model_version=manifest.model_version, training_version=manifest.training_version)
    try:
        return expectation.validate()
    except ModelBackendError:
        _fail()


def _validate_runtime_block(runtime: Any) -> Mapping[str, Any]:
    if type(runtime) is not dict or runtime.keys() != _RUNTIME_KEYS:
        _fail("MANIFEST_INVALID")
    for name, maximum in (("os_name", 64), ("python_implementation", 64),
                          ("python_version", 32), ("python_cache_tag", 32),
                          ("torch_version", 64), ("torch_cuda", 32),
                          ("native_filename", 260), ("compiler", 64)):
        _bounded_text(runtime.get(name), maximum=maximum)
    if (not runtime["native_filename"].endswith((".pyd", ".so"))
            or any(char in runtime["native_filename"] for char in '<>:"/\\|?*')):
        _fail("MANIFEST_INVALID")
    for name in ("native_sha256", "native_source_sha256", "identity_sha256"):
        if not _is_hex64(runtime.get(name)):
            _fail("MANIFEST_INVALID")
    if (type(runtime["native_bytes"]) is not int or not 0 < runtime["native_bytes"] < 2**31
            or type(runtime["pointer_bits"]) is not int or runtime["pointer_bits"] not in (32, 64)
            or type(runtime["cxx_standard"]) is not int or runtime["cxx_standard"] < 201103):
        _fail("MANIFEST_INVALID")
    if runtime["backend"] != "cpp" or runtime["protocol_version"] != PROTOCOL:
        _fail()
    return MappingProxyType(dict(runtime))


def _validate_evaluation(value: Any) -> Mapping[str, Any]:
    if type(value) is not dict or set(value) != set(_EVALUATION):
        _fail("MANIFEST_INVALID")
    for name, expected in _EVALUATION.items():
        if type(value[name]) is not type(expected) or value[name] != expected:
            _fail()
    return MappingProxyType(dict(value))


def _validate_readiness(value: Any) -> Mapping[str, Any]:
    if type(value) is not dict or set(value) != set(_READINESS):
        _fail("MANIFEST_INVALID")
    for name in ("experimental", "test_only", "production_ready", "public_self_supported"):
        if type(value[name]) is not type(_READINESS[name]) or value[name] != _READINESS[name]:
            _fail()
    if value["live_eligible_set"] != []:
        _fail("MANIFEST_INVALID")
    return MappingProxyType({**value, "live_eligible_set": tuple()})


@dataclass(frozen=True, slots=True)
class DeploymentDescriptor:
    checkpoint: Mapping[str, Any]
    checkpoint_expectation: CheckpointExpectation
    source_fingerprint: Mapping[str, Any]
    runtime: Mapping[str, Any]
    evaluation: Mapping[str, Any]
    readiness: Mapping[str, Any]
    bundle_fingerprint: str


@dataclass(frozen=True, slots=True)
class ResolvedDeployment:
    descriptor: DeploymentDescriptor
    checkpoint_bundle: ModelBundle
    source_root: Path
    deployment_identity: DeploymentIdentity
    bootstrap: "VerifiedPackageBootstrap"


def build_deployment(*, checkpoint: CheckpointExpectation, source_fingerprint: Mapping[str, Any],
                     runtime: Mapping[str, Any]) -> dict[str, Any]:
    """Create the exact bounded descriptor for one bundle; callers decide where to save it."""
    expectation = checkpoint.validate()
    verified = _validate_source_fingerprint(dict(source_fingerprint))
    block = {
        "filename": expectation.filename, "bytes": expectation.bytes, "sha256": expectation.sha256,
        "schema": expectation.schema, "algorithm": expectation.algorithm,
        "architecture": expectation.architecture, "iteration": expectation.iteration,
        "protocol_version": expectation.protocol_version,
        "classification": expectation.classification,
        "resume_classification": expectation.resume_classification,
        "training_resume_allowed": expectation.training_resume_allowed,
        "resolved_config_hash": expectation.resolved_config_hash,
        "source_hash": expectation.source_hash,
        "source_parent_commit": expectation.source_parent_commit,
        "training_backend_identity_hash": expectation.training_backend_identity_hash,
        "payload_variant": expectation.payload_variant,
    }
    return {
        "schema": SCHEMA, "checkpoint": block, "source_fingerprint": dict(verified),
        "runtime": dict(runtime), "evaluation": dict(_EVALUATION), "readiness": dict(_READINESS),
        "source_environment": SOURCE_ENV, "checkpoint_environment": CHECKPOINT_ENV,
    }


def provider_manifest(artifact: str, artifact_sha256: str, *, model_version: str,
                      training_version: str) -> ModelManifest:
    from .identity import CONTRACT_FIELDS  # local import keeps module import surface small
    return ModelManifest.from_dict({
        "schema_version": 1, "protocol_version": 1, "artifact": artifact,
        **dict(CONTRACT_FIELDS), "model_version": model_version,
        "training_version": training_version, "artifact_format": DEPLOYMENT_ARTIFACT_FORMAT,
        "artifact_sha256": artifact_sha256,
        "capabilities": {"recommend": True, "next_token_policy": True},
    })


def load_descriptor(bundle: ModelBundle) -> DeploymentDescriptor:
    validate_provider_manifest(bundle.manifest)
    bundle.verify()
    try:
        raw = bundle.artifact_path.read_bytes()
        if len(raw) > MAX_MANIFEST_BYTES:
            _fail("MANIFEST_INVALID")
        data = strict_json(raw.decode("utf-8"))
    except ModelBackendError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise ModelBackendError("MANIFEST_INVALID") from None
    if type(data) is not dict or data.keys() != _TOP_KEYS or data.get("schema") != SCHEMA:
        _fail("MANIFEST_INVALID")
    if data.get("source_environment") != SOURCE_ENV or data.get("checkpoint_environment") != CHECKPOINT_ENV:
        _fail("MANIFEST_INVALID")
    expectation = _validate_checkpoint_block(data["checkpoint"], bundle.manifest)
    runtime = _validate_runtime_block(data["runtime"])
    return DeploymentDescriptor(
        checkpoint=MappingProxyType(dict(data["checkpoint"])),
        checkpoint_expectation=expectation,
        source_fingerprint=_validate_source_fingerprint(data["source_fingerprint"]),
        runtime=runtime, evaluation=_validate_evaluation(data["evaluation"]),
        readiness=_validate_readiness(data["readiness"]),
        bundle_fingerprint=bundle.manifest.artifact_sha256,
    )


def _verify_source_tree(root: Path, fingerprint: Mapping[str, Any]) -> None:
    if not root.is_dir():
        raise ModelBackendError("MODEL_UNAVAILABLE")
    _reject_reparse_chain(root)
    _reject_forbidden_source_entries(root)
    for component in fingerprint["components"].values():
        for entry in component["files"]:
            relative = _safe_relative(entry["path"])
            lexical = root.joinpath(*PurePosixPath(relative).parts)
            _reject_reparse_chain(lexical)
            path = lexical.resolve()
            if not path.is_relative_to(root.resolve()) or not path.is_file():
                raise ModelBackendError("MODEL_UNAVAILABLE")
            try:
                raw = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            except OSError:
                raise ModelBackendError("MODEL_UNAVAILABLE") from None
            if (len(raw), hashlib.sha256(raw).hexdigest()) != (entry["bytes"], entry["sha256"]):
                raise ModelBackendError("MODEL_VERSION_UNSUPPORTED")


@dataclass(slots=True)
class _LockedNativeArtifact:
    path: Path
    stream: Any
    sha256: str
    bytes: int

    def close(self) -> None:
        self.stream.close()


def _windows_final_path(handle: int) -> Path:
    import ctypes
    import ctypes.wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel32.GetFinalPathNameByHandleW
    function.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.LPWSTR,
                         ctypes.wintypes.DWORD, ctypes.wintypes.DWORD]
    function.restype = ctypes.wintypes.DWORD
    size = function(handle, None, 0, 0)
    if not size:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_unicode_buffer(size + 1)
    if not function(handle, buffer, len(buffer), 0):
        raise ctypes.WinError(ctypes.get_last_error())
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value).resolve()


def _lock_native_artifact(path: Path) -> _LockedNativeArtifact:
    if os.name != "nt":
        raise ModelBackendError("RUNTIME_UNSUPPORTED")
    _reject_reparse_chain(path)
    import ctypes
    import ctypes.wintypes
    import msvcrt
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = [ctypes.wintypes.LPCWSTR, ctypes.wintypes.DWORD,
                       ctypes.wintypes.DWORD, ctypes.c_void_p, ctypes.wintypes.DWORD,
                       ctypes.wintypes.DWORD, ctypes.wintypes.HANDLE]
    create.restype = ctypes.wintypes.HANDLE
    handle = create(str(path), 0x80000000, 0x00000001, None, 3, 0x00000080, None)
    invalid = ctypes.wintypes.HANDLE(-1).value
    if handle == invalid:
        raise ModelBackendError("MODEL_UNAVAILABLE")
    try:
        if _windows_final_path(handle) != path.resolve():
            raise ModelBackendError("ARTIFACT_PATH_INVALID")
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        handle = None
        stream = os.fdopen(fd, "rb", closefd=True)
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        size = stream.tell()
        stream.seek(0)
        return _LockedNativeArtifact(path.resolve(), stream, digest, size)
    except Exception:
        if handle is not None:
            kernel32.CloseHandle(handle)
        raise


@dataclass(slots=True)
class _VerifiedPythonLoader(importlib.abc.Loader):
    path: Path
    expected_bytes: int
    expected_sha256: str

    def create_module(self, spec):
        return None

    def exec_module(self, module) -> None:
        try:
            raw = self.path.read_bytes()
        except OSError:
            raise ModelBackendError("MODEL_UNAVAILABLE") from None
        normalized = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        if (len(normalized) != self.expected_bytes
                or hashlib.sha256(normalized).hexdigest() != self.expected_sha256):
            raise ModelBackendError("MODEL_VERSION_UNSUPPORTED")
        try:
            code = compile(raw, str(self.path), "exec", dont_inherit=True)
            exec(code, module.__dict__)
        except ModelBackendError:
            raise
        except BaseException:
            raise ModelBackendError("MODEL_UNAVAILABLE") from None


@dataclass(slots=True)
class _VerifiedPackageFinder(importlib.abc.MetaPathFinder):
    source_root: Path
    modules: dict[str, tuple[Path, bool, int, str]]
    native_path: Path
    previous_module_names: frozenset[str]

    @classmethod
    def create(cls, source_root: Path, fingerprint: Mapping[str, Any], *,
               native_path: Path):
        modules: dict[str, tuple[Path, bool, int, str]] = {}
        for component in fingerprint["components"].values():
            for entry in component["files"]:
                relative = str(entry["path"]).replace("\\", "/")
                if not relative.endswith(".py"):
                    continue
                if not relative.startswith(("guandan_marvel/", "nanopokerlib/")):
                    continue
                module_name = relative[:-3].replace("/", ".")
                is_package = module_name.endswith(".__init__")
                if is_package:
                    module_name = module_name[:-len(".__init__")]
                path = (source_root / Path(*relative.split("/"))).resolve()
                modules[module_name] = (path, is_package, entry["bytes"], entry["sha256"])
        return cls(source_root, modules, native_path, frozenset(sys.modules))

    def find_spec(self, fullname: str, path=None, target=None):
        item = self.modules.get(fullname)
        if item is not None:
            module_path, is_package, expected_bytes, expected_sha256 = item
            loader = _VerifiedPythonLoader(module_path, expected_bytes, expected_sha256)
            if is_package:
                return importlib.util.spec_from_file_location(
                    fullname, module_path, loader=loader,
                    submodule_search_locations=[str(module_path.parent)],
                )
            return importlib.util.spec_from_file_location(fullname, module_path, loader=loader)
        if fullname == "nanopokerlib._guandan_cpp":
            return importlib.util.spec_from_file_location(fullname, self.native_path)
        if fullname == "guandan_marvel" or fullname.startswith("guandan_marvel."):
            raise ModuleNotFoundError(fullname)
        if fullname == "nanopokerlib" or fullname.startswith("nanopokerlib."):
            raise ModuleNotFoundError(fullname)
        return None

    def close(self) -> None:
        try:
            sys.meta_path.remove(self)
        except ValueError:
            pass
        for name in tuple(sys.modules):
            if name in self.previous_module_names:
                continue
            if name in self.modules or name.startswith(("guandan_marvel.", "nanopokerlib.")):
                sys.modules.pop(name, None)


@dataclass(slots=True)
class VerifiedPackageBootstrap:
    finder: _VerifiedPackageFinder

    def close(self) -> None:
        self.finder.close()


def _module_origin(name: str) -> Path:
    module = sys.modules.get(name)
    origin = getattr(module, "__file__", None) if module is not None else None
    if not isinstance(origin, str) or not origin:
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, AttributeError, ValueError):
            spec = None
        origin = getattr(spec, "origin", None)
    if not isinstance(origin, str) or not origin:
        raise ModelBackendError("MODEL_VERSION_UNSUPPORTED")
    return Path(origin).resolve()


def _bootstrap_verified_packages(
        source_root: Path, fingerprint: Mapping[str, Any], *,
        native_path: Path) -> VerifiedPackageBootstrap:
    if str(source_root) in getattr(sys, "path"):
        raise ModelBackendError("MODEL_VERSION_UNSUPPORTED")
    for package in ("guandan_marvel", "nanopokerlib"):
        existing = sys.modules.get(package)
        if existing is not None:
            existing_path = Path(str(getattr(existing, "__file__", ""))).resolve()
            if existing_path != (source_root / package / "__init__.py").resolve():
                raise ModelBackendError("MODEL_VERSION_UNSUPPORTED")
    finder = _VerifiedPackageFinder.create(
        source_root, fingerprint, native_path=native_path,
    )
    sys.meta_path.insert(0, finder)
    try:
        importlib.import_module("guandan_marvel")
        importlib.import_module("nanopokerlib")
        expected = {
            "guandan_marvel": source_root / "guandan_marvel/__init__.py",
            "nanopokerlib": source_root / "nanopokerlib/__init__.py",
        }
        for name, path in expected.items():
            if _module_origin(name) != path.resolve():
                raise ModelBackendError("MODEL_VERSION_UNSUPPORTED")
        return VerifiedPackageBootstrap(finder)
    except BaseException:
        finder.close()
        if isinstance(sys.exc_info()[1], ModelBackendError):
            raise
        raise ModelBackendError("MODEL_UNAVAILABLE") from None


def resolve_environment(descriptor: DeploymentDescriptor) -> ResolvedDeployment:
    source_value = os.environ.get(SOURCE_ENV, "").strip()
    checkpoint_value = os.environ.get(CHECKPOINT_ENV, "").strip()
    if not source_value or not checkpoint_value:
        raise ModelBackendError("MODEL_UNAVAILABLE")
    source_root = _strict_external_path(source_value, directory=True)
    checkpoint_path = _strict_external_path(checkpoint_value, directory=False)
    runtime = descriptor.runtime
    if checkpoint_path.name != descriptor.checkpoint["filename"]:
        raise ModelBackendError("ARTIFACT_MISSING")
    checkpoint_sha, checkpoint_bytes = _sha256_file(checkpoint_path)
    if (checkpoint_sha != descriptor.checkpoint["sha256"]
            or checkpoint_bytes != descriptor.checkpoint["bytes"]):
        raise ModelBackendError("ARTIFACT_HASH_MISMATCH")
    _verify_source_tree(source_root, descriptor.source_fingerprint)
    expected_native_path = source_root / "nanopokerlib" / str(runtime["native_filename"])
    _reject_reparse_chain(expected_native_path)
    locked_native = _lock_native_artifact(expected_native_path.resolve())
    if (locked_native.sha256 != runtime["native_sha256"]
            or locked_native.bytes != runtime["native_bytes"]):
        locked_native.close()
        raise ModelBackendError("ARTIFACT_HASH_MISMATCH")
    try:
        bootstrap = _bootstrap_verified_packages(
            source_root, descriptor.source_fingerprint, native_path=locked_native.path,
        )
    except Exception:
        locked_native.close()
        raise
    try:
        torch = importlib.import_module("torch")
        native = importlib.import_module("nanopokerlib.guandan")
        config_module = importlib.import_module("guandan_marvel.config")
        model_module = importlib.import_module("guandan_marvel.model")
        build = native.get_backend_build_identity()
        _verify_source_tree(source_root, descriptor.source_fingerprint)
        if (platform.system() != runtime["os_name"]
                or platform.python_implementation() != runtime["python_implementation"]
                or platform.python_version() != runtime["python_version"]
                or (sys.implementation.cache_tag or "") != runtime["python_cache_tag"]
                or struct.calcsize("P") * 8 != runtime["pointer_bits"]
                or str(torch.__version__) != runtime["torch_version"]
                or str(torch.version.cuda or "cpu") != runtime["torch_cuda"]):
            raise ModelBackendError("RUNTIME_UNSUPPORTED")
        if (not isinstance(build, Mapping)
                or build.get("extension_filename") != runtime["native_filename"]
                or build.get("extension_sha256") != runtime["native_sha256"]
                or build.get("embedded_source_sha256") != runtime["native_source_sha256"]
                or build.get("compiler") != runtime["compiler"]
                or build.get("cxx_standard") != runtime["cxx_standard"]
                or build.get("python_cache_tag") != runtime["python_cache_tag"]
                or build.get("source_matches_loaded_binary") is not True):
            raise ModelBackendError("MODEL_VERSION_UNSUPPORTED")
        for module in (config_module, model_module):
            module_path = Path(str(getattr(module, "__file__", ""))).resolve()
            if not module_path.is_relative_to(source_root):
                raise ModelBackendError("MODEL_VERSION_UNSUPPORTED")
        native_path = Path(str(build.get("extension_path", ""))).resolve()
        if native_path != expected_native_path.resolve():
            raise ModelBackendError("MODEL_VERSION_UNSUPPORTED")
        native_sha, native_bytes = locked_native.sha256, locked_native.bytes
        identity = DeploymentIdentity(
            os_name=platform.system(), os_release=platform.release(),
            python_implementation=platform.python_implementation(), python_version=platform.python_version(),
            python_cache_tag=sys.implementation.cache_tag or "", pointer_bits=struct.calcsize("P") * 8,
            torch_version=str(torch.__version__), torch_cuda=str(torch.version.cuda or "cpu"),
            native_path=str(native_path), native_sha256=native_sha, native_bytes=native_bytes,
            native_source_sha256=str(build["embedded_source_sha256"]), compiler=str(build["compiler"]),
            cxx_standard=int(build["cxx_standard"]), backend="cpp", protocol_version=PROTOCOL,
        )
        identity.validate(); require_platform_distinct_from_training(identity)
        if identity.identity_sha256 != runtime["identity_sha256"]:
            raise ModelBackendError("MODEL_VERSION_UNSUPPORTED")
        expectation = descriptor.checkpoint_expectation
        checkpoint_bundle = ModelBundle(checkpoint_manifest(
            checkpoint_path.name, expectation.sha256,
            model_version=expectation.model_version,
            training_version=expectation.training_version), checkpoint_path.parent)
        checkpoint_bundle.verify()
        locked_native.close()
        return ResolvedDeployment(descriptor, checkpoint_bundle, source_root, identity, bootstrap)
    except ModelBackendError:
        locked_native.close()
        bootstrap.close()
        raise
    except Exception:
        locked_native.close()
        bootstrap.close()
        raise ModelBackendError("MODEL_UNAVAILABLE") from None


__all__ = [
    "CHECKPOINT_ENV", "DeploymentDescriptor", "ResolvedDeployment", "SCHEMA", "SOURCE_ENV",
    "build_deployment", "load_descriptor", "provider_manifest", "resolve_environment",
]
