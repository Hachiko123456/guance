#!/usr/bin/env python3
"""Install MARVEL V2 runtime materials and build data-driven model bundles.

Trust model (install-runtime)
-----------------------------
The installer NEVER derives expected identity from the materials it installs.
Trusted pins must be supplied explicitly (all CLI parameters are required):
``--expected-source-zip-sha256``, ``--expected-source-manifest-sha256``,
``--expected-source-content-manifest-hash``, ``--expected-source-commit``,
``--expected-native-sha256``, ``--expected-native-bytes`` and
``--expected-overlay-manifest-hash``. The canonical trust source is the
versioned trusted runtime-materials record produced by an authorized Phase 2
run and preserved as evidence (``phase2/runtime.json`` and
``phase2/phase2-four-condition-gate.json``), or an operator-supplied
expected-identity record copied from it. Pins taken from the archive or its
manifest are untrusted by definition and must not be used.

Enforcement order: input path checks, target/parent checks, then every pin
(zip file SHA-256, manifest file SHA-256, canonical manifest hash, git commit,
native identity, raw overlay manifest hash) is verified BEFORE any staging
directory is created, before any extraction, before any archive Python is
executed, before the native extension is loaded, and before the identity probe
runs.

install-runtime additionally:
  * strict-parses the source manifest (duplicate JSON members, NaN/Infinity,
    exact schema/top-level keys, canonical manifest_hash self-consistency,
    unique safe PurePosix relative paths, bounded sizes/hashes and a forbidden
    top-level shadow gate for sitecustomize/usercustomize/stdlib-shadow names);
  * validates the archive with metadata-only checks first (duplicate members,
    member count limit, declared ``file_size`` exactly equal to the manifest
    bytes, per-member 16 MiB limit, explicit total extracted-byte limit,
    compression-ratio limit, encrypted/symlink rejection) and then reads every
    member with a bounded streaming read of at most expected+1 bytes, so a
    lying or bomb member cannot be read past its declared size;
  * writes only inside a unique sibling staging directory and runs the
    identity probe in an isolated interpreter (``-I -B``, cleared
    PYTHONPATH/PYTHONHOME) whose import surface is a deny-by-default finder:
    only byte-verified fingerprint modules under ``guandan_marvel/`` and
    ``nanopokerlib/`` are executed, the native extension bytes are re-hashed
    before load, and ``source_matches_loaded_binary is True`` is still
    required;
  * re-verifies every staged hash against the trusted pins and the recorded
    fingerprint/SHA256SUMS before an atomic rename; on any failure only that
    staging directory is removed, so the target never appears and nothing
    outside it is left behind.

build-bundle
    Emits manifest.json + deployment.json + SHA256SUMS for one verified
    checkpoint. The bundle is assembled in a sibling staging directory and
    fully re-validated through the production ``load_bundle``/``load_descriptor``
    gates before an atomic rename. An existing output fails explicitly (never
    overwritten or merged); the staging name is dot-prefixed so a catalog scan
    can never discover a partial/temporary directory. The deployment descriptor
    is data-driven; no iteration/hash constant is compiled into Guance.

Both subcommands are read-only with respect to all inputs.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import uuid
import zipfile

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from guance.infrastructure.model.marvel_v2.checkpoint import (  # noqa: E402
    CheckpointExpectation, _restricted_load,
)
from guance.infrastructure.model.marvel_v2.deployment import (  # noqa: E402
    DeploymentIdentity,
)
from guance.infrastructure.model.marvel_v2.deployment_bundle import (  # noqa: E402
    _FORBIDDEN_SOURCE_ENTRIES, _FORBIDDEN_SOURCE_PACKAGES,
    build_deployment, load_descriptor, provider_manifest,
)
from guance.infrastructure.model.marvel_v2.identity import (  # noqa: E402
    CONTRACT_FIELDS, DEPLOYMENT_ARTIFACT_FORMAT,
)

NATIVE_FILENAME = "_guandan_cpp.cp312-win_amd64.pyd"
CPP_PATHS = ("nanopokerlib/cpp/bindings.cpp", "nanopokerlib/cpp/core/types.hpp",
             "nanopokerlib/cpp/guandan/guandan.cpp", "nanopokerlib/cpp/guandan/guandan.hpp")
RUNTIME_SCHEMA = "guance-marvel-v2-runtime-v1"
RUNTIME_CONTRACT = "marvel_v2.cpu.v1"

ZIP_PREFIX = "guandan-marvel-source/"
SOURCE_MANIFEST_SCHEMA = "guandan-marvel-source-manifest-v1"
SOURCE_MANIFEST_NAME = "source_manifest.json"
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_MAX_MEMBER_BYTES = 16 * 1024 * 1024
_MAX_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 128
_MAX_MEMBER_COUNT = 100_000
_MODULE_TRUST_PREFIXES = ("guandan_marvel/", "nanopokerlib/")
_MANIFEST_TOP_KEYS = frozenset({
    "schema", "source_root", "git_commit", "package_id", "created_at",
    "bundled_dependencies", "licenses", "remote_verified", "uploaded",
    "workspace_snapshot", "files", "manifest_hash",
})
_FILE_ENTRY_KEYS = frozenset({"path", "bytes", "origin", "sha256"})
_FILE_ORIGINS = frozenset({"git", "bundled_pybind11"})
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

_PROBE_CODE = r'''
import hashlib, importlib.abc, importlib.util, json, os, sys

source_root = os.path.realpath(sys.argv[1])
trust = json.loads(sys.argv[2])
modules = trust["modules"]
native = trust["native"]


def _resolve(relative):
    candidate = os.path.realpath(os.path.join(source_root, *relative.split("/")))
    if candidate != source_root and not candidate.startswith(source_root + os.sep):
        raise RuntimeError("trusted module path escapes the source root")
    return candidate


class _VerifiedLoader(importlib.abc.Loader):
    def __init__(self, path, size, digest):
        self.path, self.size, self.digest = path, size, digest

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        with open(self.path, "rb") as stream:
            raw = stream.read()
        if len(raw) != self.size or hashlib.sha256(raw).hexdigest() != self.digest:
            raise RuntimeError("trusted module bytes mismatch: " + self.path)
        exec(compile(raw, self.path, "exec", dont_inherit=True), module.__dict__)


class _TrustedFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        entry = modules.get(fullname)
        if entry is not None:
            file_path = _resolve(entry["path"])
            loader = _VerifiedLoader(file_path, entry["bytes"], entry["sha256"])
            if entry.get("package"):
                return importlib.util.spec_from_file_location(
                    fullname, file_path, loader=loader,
                    submodule_search_locations=[os.path.dirname(file_path)])
            return importlib.util.spec_from_file_location(fullname, file_path, loader=loader)
        if fullname == "nanopokerlib._guandan_cpp":
            native_path = _resolve(native["path"])
            with open(native_path, "rb") as stream:
                blob = stream.read()
            if hashlib.sha256(blob).hexdigest() != native["sha256"]:
                raise RuntimeError("trusted native bytes mismatch")
            return importlib.util.spec_from_file_location(fullname, native_path)
        if (fullname == "nanopokerlib" or fullname.startswith("nanopokerlib.")
                or fullname == "guandan_marvel" or fullname.startswith("guandan_marvel.")):
            raise ModuleNotFoundError(fullname)
        return None


sys.meta_path.insert(0, _TrustedFinder())
import nanopokerlib.guandan as guandan_native
identity = guandan_native.get_backend_build_identity()
manifest = identity["current_source_manifest"]
print(json.dumps({
    "extension_path": identity["extension_path"],
    "extension_filename": identity["extension_filename"],
    "extension_sha256": identity["extension_sha256"],
    "embedded_source_sha256": identity["embedded_source_sha256"],
    "current_source_manifest_hash": None if manifest is None else manifest["hash"],
    "current_source_manifest_files": None if manifest is None else manifest["files"],
    "source_matches_loaded_binary": identity["source_matches_loaded_binary"],
    "compiler": identity["compiler"],
    "cxx_standard": identity["cxx_standard"],
    "python_implementation": identity["python_implementation"],
    "python_version": identity["python_version"],
    "python_cache_tag": identity["python_cache_tag"],
}))
'''


def _fail(message: str) -> None:
    raise SystemExit(f"installer: {message}")


def _require(condition: bool, message: str) -> None:
    if not condition:
        _fail(message)


@dataclass(frozen=True, slots=True)
class _TrustedPins:
    source_zip_sha256: str
    source_manifest_sha256: str
    source_content_manifest_hash: str
    source_commit: str
    native_sha256: str
    native_bytes: int
    overlay_manifest_hash: str

    def validate(self) -> "_TrustedPins":
        for name in ("source_zip_sha256", "source_manifest_sha256",
                     "source_content_manifest_hash", "native_sha256",
                     "overlay_manifest_hash"):
            value = getattr(self, name)
            _require(isinstance(value, str) and _HEX64.fullmatch(value) is not None,
                     f"--expected-{name.replace('_', '-')} must be lowercase 64-hex")
        _require(isinstance(self.source_commit, str) and _COMMIT.fullmatch(self.source_commit) is not None,
                 "--expected-source-commit must be a lowercase 40-hex commit")
        _require(type(self.native_bytes) is int and 0 < self.native_bytes < 2**31,
                 "--expected-native-bytes must be a positive bounded integer")
        return self

    def as_dict(self) -> dict:
        return {
            "source_zip_sha256": self.source_zip_sha256,
            "source_manifest_sha256": self.source_manifest_sha256,
            "source_content_manifest_hash": self.source_content_manifest_hash,
            "source_commit": self.source_commit,
            "native_sha256": self.native_sha256,
            "native_bytes": self.native_bytes,
            "overlay_manifest_hash": self.overlay_manifest_hash,
        }


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256(); size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            size += len(chunk); digest.update(chunk)
    return digest.hexdigest(), size


def _read_bounded(path: Path, maximum: int, what: str) -> bytes:
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    _require(len(raw) <= maximum, f"{what} is oversized")
    return raw


def _canonical(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _strict_json_object(raw: bytes, what: str) -> object:
    def _object(pairs):
        result: dict = {}
        for key, value in pairs:
            if key in result:
                _fail(f"{what}: duplicate JSON member {key!r}")
            result[key] = value
        return result

    def _constant(value):
        _fail(f"{what}: non-finite JSON constant {value!r}")

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
                          parse_constant=_constant)
    except SystemExit:
        raise
    except (UnicodeError, ValueError, RecursionError):
        _fail(f"{what}: malformed JSON")


def _bounded_text(value: object, maximum: int, what: str) -> str:
    _require(isinstance(value, str) and 0 < len(value) <= maximum,
             f"{what} must be a bounded non-empty string")
    _require(not any(ord(char) < 32 or ord(char) == 127 for char in value),
             f"{what} contains control characters")
    return value


def _safe_relative_member(value: object, what: str) -> str:
    _require(isinstance(value, str) and 0 < len(value) <= 512, f"{what} path invalid")
    _require("\\" not in value, f"{what} path must not contain backslashes")
    _require(not any(ord(char) < 32 or ord(char) == 127 for char in value),
             f"{what} path contains control characters")
    _require(not any(char in value for char in '<>:"|?*'), f"{what} path invalid")
    path = PurePosixPath(value)
    _require(not path.is_absolute() and not PureWindowsPath(value).drive,
             f"{what} path must be relative")
    for part in value.split("/"):
        _require(part not in ("", ".", ".."), f"{what} path has an empty/dot segment")
        _require(not part.endswith((" ", ".")), f"{what} path has a trailing space/dot")
        _require(not PureWindowsPath(part).is_reserved(), f"{what} path is reserved")
    return value


def _checked_manifest_path(value: object, what: str) -> str:
    relative = _safe_relative_member(value, what)
    top = relative.split("/", 1)[0]
    _require(top not in _FORBIDDEN_SOURCE_ENTRIES,
             f"forbidden top-level shadow entry: {relative}")
    _require(top not in _FORBIDDEN_SOURCE_PACKAGES,
             f"forbidden top-level shadow package: {relative}")
    return relative


def _reject_reparse_chain(path: Path) -> None:
    for candidate in reversed([path, *path.parents]):
        if not candidate.exists():
            continue
        try:
            info = os.lstat(candidate)
        except OSError:
            _fail(f"cannot inspect path: {candidate}")
        if candidate.is_symlink() or int(getattr(info, "st_file_attributes", 0)) & _REPARSE_ATTRIBUTE:
            _fail(f"symlink/junction/reparse point rejected: {candidate}")


def _load_source_manifest(raw: bytes) -> dict:
    _require(len(raw) <= _MAX_MANIFEST_BYTES, "source manifest is oversized")
    data = _strict_json_object(raw, "source manifest")
    _require(type(data) is dict, "source manifest must be a JSON object")
    _require(frozenset(data.keys()) == _MANIFEST_TOP_KEYS,
             "source manifest top-level keys are not the exact expected set")
    _require(data["schema"] == SOURCE_MANIFEST_SCHEMA, "source manifest schema mismatch")
    _require(data["source_root"] == "guandan-marvel-source", "source manifest source_root mismatch")
    _require(isinstance(data["git_commit"], str) and _COMMIT.fullmatch(data["git_commit"]) is not None,
             "source manifest git_commit invalid")
    _bounded_text(data["package_id"], 128, "package_id")
    _bounded_text(data["created_at"], 64, "created_at")
    for flag in ("remote_verified", "uploaded", "workspace_snapshot"):
        _require(type(data[flag]) is bool, f"source manifest {flag} must be boolean")
    _require(isinstance(data["manifest_hash"], str) and _HEX64.fullmatch(data["manifest_hash"]) is not None,
             "source manifest manifest_hash invalid")
    recomputed = _canonical({key: value for key, value in data.items() if key != "manifest_hash"})
    _require(recomputed == data["manifest_hash"],
             "source manifest manifest_hash is not self-consistent")
    dependencies = data["bundled_dependencies"]
    _require(isinstance(dependencies, list) and len(dependencies) <= 64,
             "source manifest bundled_dependencies invalid")
    for item in dependencies:
        _require(type(item) is dict and set(item.keys()) == {"name", "purpose", "version"},
                 "source manifest bundled_dependencies entry invalid")
        for key in ("name", "purpose", "version"):
            _bounded_text(item[key], 128, f"bundled_dependencies.{key}")
    licenses = data["licenses"]
    _require(isinstance(licenses, list) and len(licenses) <= 256, "source manifest licenses invalid")
    seen_licenses: set[str] = set()
    for value in licenses:
        relative = _checked_manifest_path(value, "licenses")
        _require(relative not in seen_licenses, "source manifest duplicate license path")
        seen_licenses.add(relative)
    files = data["files"]
    _require(isinstance(files, list) and 0 < len(files) <= _MAX_MEMBER_COUNT,
             "source manifest files must be a non-empty bounded list")
    seen_paths: set[str] = set()
    for entry in files:
        _require(type(entry) is dict and frozenset(entry.keys()) == _FILE_ENTRY_KEYS,
                 "source manifest file entry keys are not the exact expected set")
        relative = _checked_manifest_path(entry["path"], "files")
        _require(relative not in seen_paths, f"source manifest duplicate file path: {relative}")
        seen_paths.add(relative)
        _require(type(entry["bytes"]) is int and 0 <= entry["bytes"] <= _MAX_MEMBER_BYTES,
                 f"source manifest file size out of bounds: {relative}")
        _require(isinstance(entry["sha256"], str) and _HEX64.fullmatch(entry["sha256"]) is not None,
                 f"source manifest file sha256 invalid: {relative}")
        _require(entry["origin"] in _FILE_ORIGINS,
                 f"source manifest file origin unsupported: {relative}")
    return data


def _bounded_member_bytes(archive: "zipfile.ZipFile", info: "zipfile.ZipInfo",
                          expected_bytes: int, what: str) -> bytes:
    chunks: list[bytes] = []
    remaining = expected_bytes + 1
    with archive.open(info) as stream:
        while remaining > 0:
            chunk = stream.read(min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    data = b"".join(chunks)
    _require(len(data) == expected_bytes, f"{what}: bounded read size mismatch")
    return data


def _validated_archive_payloads(zip_path: Path, manifest: dict, raw_manifest: bytes) -> dict[str, bytes]:
    expected = {entry["path"]: (entry["bytes"], entry["sha256"]) for entry in manifest["files"]}
    with zipfile.ZipFile(zip_path) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        _require(len(names) == len(set(names)), "archive contains duplicate members")
        _require(len(infos) <= _MAX_MEMBER_COUNT, "archive member count exceeds the limit")
        infos_by_relative: dict[str, "zipfile.ZipInfo"] = {}
        total_bytes = 0
        for info in infos:
            name = info.filename
            _require(not name.endswith("/"), f"archive directory member not allowed: {name}")
            _require(name.startswith(ZIP_PREFIX), f"unexpected archive member: {name}")
            relative = _safe_relative_member(name[len(ZIP_PREFIX):], "archive member")
            mode = (info.external_attr >> 16) & 0xFFFF
            _require(not (mode and stat.S_ISLNK(mode)), f"archive symlink member not allowed: {name}")
            _require((info.flag_bits & 0x1) == 0, f"archive encrypted member not allowed: {name}")
            if relative == SOURCE_MANIFEST_NAME:
                declared = len(raw_manifest)
            else:
                _require(relative in expected, f"archive member not listed in the manifest: {relative}")
                declared = expected[relative][0]
            _require(info.file_size == declared,
                     f"archive member declared size mismatch: {relative}")
            _require(declared <= _MAX_MEMBER_BYTES,
                     f"archive member exceeds the size limit: {relative}")
            if declared > 0:
                _require(0 < info.compress_size <= _MAX_MEMBER_BYTES,
                         f"archive member compressed size invalid: {relative}")
                _require(declared <= info.compress_size * _MAX_COMPRESSION_RATIO,
                         f"archive member compression ratio exceeds the limit: {relative}")
            total_bytes += declared
            _require(total_bytes <= _MAX_TOTAL_BYTES,
                     "archive total uncompressed size exceeds the limit")
            infos_by_relative[relative] = info
        _require(set(infos_by_relative) == set(expected) | {SOURCE_MANIFEST_NAME},
                 "archive member set does not exactly match the manifest")
        payloads: dict[str, bytes] = {}
        for relative, info in infos_by_relative.items():
            declared = len(raw_manifest) if relative == SOURCE_MANIFEST_NAME else expected[relative][0]
            payloads[relative] = _bounded_member_bytes(archive, info, declared,
                                                       f"archive member {relative}")
        _require(payloads[SOURCE_MANIFEST_NAME] == raw_manifest,
                 "embedded source_manifest.json differs from the external manifest")
        for relative, (size, digest) in expected.items():
            member = payloads[relative]
            _require(len(member) == size and hashlib.sha256(member).hexdigest() == digest,
                     f"archive member bytes/hash mismatch: {relative}")
    return {relative: payloads[relative] for relative in expected}


def _extract_validated(source_root: Path, payloads: dict[str, bytes]) -> None:
    base = source_root.resolve()
    for relative, payload in payloads.items():
        destination = source_root.joinpath(*relative.split("/"))
        resolved = destination.resolve()
        _require(resolved.is_relative_to(base), f"archive member escapes the source root: {relative}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)


def _raw_native_manifest(root: Path) -> dict:
    files = []
    for relative in CPP_PATHS:
        data = (root / relative).read_bytes()
        files.append({"path": relative, "bytes": len(data),
                      "sha256": hashlib.sha256(data).hexdigest()})
    payload = {"schema": "guandan-native-build-identity-v1", "files": files}
    return {**payload, "hash": _canonical(payload)}


def _runtime_source_fingerprint(root: Path) -> dict:
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative.startswith(".") or "__pycache__" in relative:
            continue
        if path.suffix in (".pyd", ".so", ".pyc"):
            continue
        raw = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        files.append({"path": relative, "bytes": len(raw),
                      "sha256": hashlib.sha256(raw).hexdigest()})
    component = {"files": files, "hash": _canonical(files)}
    payload = {"schema": "guandan-marvel-source-fingerprint-v1",
               "components": {"materialized-runtime": component}}
    return {**payload, "hash": _canonical(payload)}


def _trusted_module_payload(manifest: dict, native_sha256: str) -> str:
    modules: dict[str, dict] = {}
    for entry in manifest["files"]:
        relative = entry["path"]
        if not relative.endswith(".py") or not relative.startswith(_MODULE_TRUST_PREFIXES):
            continue
        name = relative[:-3].replace("/", ".")
        is_package = name.endswith(".__init__")
        if is_package:
            name = name[: -len(".__init__")]
        modules[name] = {"path": relative, "bytes": int(entry["bytes"]),
                         "sha256": entry["sha256"], "package": is_package}
    payload = json.dumps({"modules": modules,
                          "native": {"path": f"nanopokerlib/{NATIVE_FILENAME}",
                                     "sha256": native_sha256}},
                         separators=(",", ":"))
    _require(len(payload) <= 200_000, "trusted module payload is oversized")
    return payload


def _identity_probe(source_root: Path, trusted_modules: str) -> dict:
    env = {key: value for key, value in os.environ.items()
           if key not in ("PYTHONPATH", "PYTHONHOME")}
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1", PYTHONSAFEPATH="1")
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", _PROBE_CODE, str(source_root), trusted_modules],
        capture_output=True, text=True, env=env, check=False)
    if completed.returncode != 0:
        raise SystemExit(f"identity probe failed: {completed.stderr.strip()[:400]}")
    for line in reversed(completed.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise SystemExit("identity probe produced no JSON output")


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def _write_sums(root: Path, output: Path) -> None:
    lines = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path == output:
            continue
        digest, _size = _sha256_file(path)
        lines.append(f"{digest}  {path.relative_to(root).as_posix()}")
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _verify_sums(root: Path) -> None:
    sums_path = root / "SHA256SUMS"
    _require(sums_path.is_file(), "SHA256SUMS missing")
    listed: dict[str, str] = {}
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, separator, name = line.partition("  ")
        _require(separator == "  " and _HEX64.fullmatch(digest) is not None and name,
                 f"SHA256SUMS line invalid: {line!r}")
        _require(name not in listed, f"SHA256SUMS duplicate entry: {name}")
        listed[name] = digest
    actual = sorted(path.relative_to(root).as_posix() for path in root.rglob("*")
                    if path.is_file() and path != sums_path)
    _require(set(listed) == set(actual), "SHA256SUMS coverage mismatch")
    for name in actual:
        digest, _size = _sha256_file(root / name)
        _require(digest == listed[name], f"SHA256SUMS mismatch: {name}")


def _final_verify_staging(staging: Path, runtime_json: dict) -> None:
    pins = runtime_json["trusted_pins"]
    source_record = runtime_json["source"]
    _require(source_record["archive_sha256"] == pins["source_zip_sha256"]
             and source_record["manifest_sha256"] == pins["source_manifest_sha256"]
             and source_record["content_manifest_hash"] == pins["source_content_manifest_hash"]
             and source_record["git_commit"] == pins["source_commit"],
             "staged source record does not match the trusted pins")
    source_root = staging / "source"
    fingerprint = _runtime_source_fingerprint(source_root)
    _require(fingerprint["hash"] == runtime_json["source_fingerprint"]["hash"],
             "staged source fingerprint mismatch")
    native_sha, native_bytes = _sha256_file(source_root / "nanopokerlib" / NATIVE_FILENAME)
    _require((native_sha, native_bytes) == (runtime_json["native"]["sha256"],
                                            runtime_json["native"]["bytes"]),
             "staged native bytes/hash mismatch")
    _require(native_sha == pins["native_sha256"] and native_bytes == pins["native_bytes"],
             "staged native does not match the trusted pins")
    overlay = _raw_native_manifest(source_root)
    _require(overlay["hash"] == runtime_json["native_source_overlay"]["raw_manifest_hash"],
             "staged raw overlay manifest mismatch")
    _require(overlay["hash"] == pins["overlay_manifest_hash"],
             "staged overlay does not match the trusted pins")
    _require(overlay["hash"] == runtime_json["native"]["embedded_source_sha256"],
             "staged overlay differs from the embedded source identity")
    _verify_sums(staging)


def _lexical(path: Path) -> Path:
    return Path(os.path.abspath(str(path)))


def install_runtime(args: argparse.Namespace) -> int:
    pins = _TrustedPins(
        source_zip_sha256=args.expected_source_zip_sha256,
        source_manifest_sha256=args.expected_source_manifest_sha256,
        source_content_manifest_hash=args.expected_source_content_manifest_hash,
        source_commit=args.expected_source_commit,
        native_sha256=args.expected_native_sha256,
        native_bytes=args.expected_native_bytes,
        overlay_manifest_hash=args.expected_overlay_manifest_hash,
    ).validate()

    zip_path = _lexical(args.source_zip)
    manifest_path = _lexical(args.source_manifest)
    overlay_root = _lexical(args.overlay_root)
    native_path = _lexical(args.native)
    target = _lexical(args.target)
    for label, path in (("source zip", zip_path), ("source manifest", manifest_path),
                        ("overlay root", overlay_root), ("native", native_path)):
        _reject_reparse_chain(path)
    _require(zip_path.is_file(), f"source zip missing: {zip_path}")
    _require(manifest_path.is_file(), f"source manifest missing: {manifest_path}")
    _require(overlay_root.is_dir(), f"overlay root missing: {overlay_root}")
    _require(native_path.is_file(), f"native extension missing: {native_path}")
    _reject_reparse_chain(target)
    _require(not target.exists(), f"target already exists: {target}")
    parent = target.parent
    _require(parent.is_dir(), f"target parent must already exist: {parent}")
    _reject_reparse_chain(parent)

    # Trusted pins gate: nothing is staged, extracted, imported or probed first.
    archive_sha, archive_bytes = _sha256_file(zip_path)
    _require(archive_sha == pins.source_zip_sha256, "trusted source zip sha256 mismatch")
    raw_manifest = _read_bounded(manifest_path, _MAX_MANIFEST_BYTES, "source manifest")
    manifest_sha = hashlib.sha256(raw_manifest).hexdigest()
    _require(manifest_sha == pins.source_manifest_sha256, "trusted source manifest sha256 mismatch")
    manifest = _load_source_manifest(raw_manifest)
    _require(manifest["manifest_hash"] == pins.source_content_manifest_hash,
             "trusted content manifest hash mismatch")
    _require(manifest["git_commit"] == pins.source_commit, "trusted source commit mismatch")
    native_input_sha, native_input_bytes = _sha256_file(native_path)
    _require((native_input_sha, native_input_bytes) == (pins.native_sha256, pins.native_bytes),
             "trusted native identity mismatch")

    overlay_files = []
    for relative in CPP_PATHS:
        overlay_path = overlay_root.joinpath(*relative.split("/"))
        _require(overlay_path.is_file(), f"overlay input missing: {relative}")
        data = overlay_path.read_bytes()
        overlay_files.append({"path": relative, "bytes": len(data),
                              "sha256": hashlib.sha256(data).hexdigest()})
    overlay_manifest = {"schema": "guandan-native-build-identity-v1", "files": overlay_files}
    overlay_manifest["hash"] = _canonical(overlay_manifest)
    _require(overlay_manifest["hash"] == pins.overlay_manifest_hash,
             "trusted raw overlay manifest hash mismatch")
    entries = {row["path"]: (row["bytes"], row["sha256"]) for row in manifest["files"]}

    payloads = _validated_archive_payloads(zip_path, manifest, raw_manifest)

    staging = parent / f".{target.name}.staging-{uuid.uuid4().hex}"
    _require(not staging.exists(), "staging name collision")
    staging.mkdir()
    try:
        source_root = staging / "source"
        source_root.mkdir()
        _extract_validated(source_root, payloads)
        for relative in CPP_PATHS:
            destination = source_root.joinpath(*relative.split("/"))
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(overlay_root.joinpath(*relative.split("/")), destination)
        native_destination = source_root / "nanopokerlib" / NATIVE_FILENAME
        native_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(native_path, native_destination)
        copied_native_sha, copied_native_bytes = _sha256_file(native_destination)
        _require((copied_native_sha, copied_native_bytes) == (native_input_sha, native_input_bytes),
                 "staged native copy mismatch")

        probe = _identity_probe(source_root, _trusted_module_payload(manifest, copied_native_sha))
        _require(probe.get("source_matches_loaded_binary") is True,
                 "source_matches_loaded_binary is not true after assembly")
        _require(probe.get("current_source_manifest_hash") == probe.get("embedded_source_sha256"),
                 "raw native source manifest does not equal the embedded identity")
        _require(probe.get("current_source_manifest_hash") == overlay_manifest["hash"],
                 "overlay raw manifest differs from the measured manifest")
        _require(probe.get("extension_filename") == NATIVE_FILENAME
                 and probe.get("extension_sha256") == copied_native_sha,
                 "loaded native extension identity mismatch")
        _require(Path(str(probe.get("extension_path", ""))).resolve() == native_destination.resolve(),
                 "loaded extension path is not the staged native")

        fingerprint = _runtime_source_fingerprint(source_root)
        native_sha, native_bytes = copied_native_sha, copied_native_bytes
        import torch  # local import: only the installer needs the runtime stack
        identity = DeploymentIdentity(
            os_name=platform.system(), os_release=platform.release(),
            python_implementation=platform.python_implementation(),
            python_version=platform.python_version(),
            python_cache_tag=sys.implementation.cache_tag or "",
            pointer_bits=struct.calcsize("P") * 8,
            torch_version=str(torch.__version__), torch_cuda=str(torch.version.cuda or "cpu"),
            native_path=str(native_destination),
            native_sha256=native_sha, native_bytes=native_bytes,
            native_source_sha256=probe["embedded_source_sha256"],
            compiler=probe["compiler"], cxx_standard=int(probe["cxx_standard"]),
            backend="cpp")
        identity.validate()
        runtime_block = {
            "os_name": identity.os_name, "python_implementation": identity.python_implementation,
            "python_version": identity.python_version, "python_cache_tag": identity.python_cache_tag,
            "pointer_bits": identity.pointer_bits, "torch_version": identity.torch_version,
            "torch_cuda": identity.torch_cuda, "native_filename": NATIVE_FILENAME,
            "native_sha256": native_sha, "native_bytes": native_bytes,
            "native_source_sha256": probe["embedded_source_sha256"], "compiler": probe["compiler"],
            "cxx_standard": int(probe["cxx_standard"]), "backend": "cpp",
            "protocol_version": identity.protocol_version, "identity_sha256": identity.identity_sha256,
        }
        runtime_id = f"marvel-{manifest['git_commit'][:12]}-{native_sha[:12]}"
        runtime_json = {
            "schema": RUNTIME_SCHEMA, "runtime_id": runtime_id, "runtime_contract": RUNTIME_CONTRACT,
            "source": {
                "archive": zip_path.name, "archive_sha256": archive_sha, "archive_bytes": archive_bytes,
                "manifest_sha256": manifest_sha, "content_manifest_hash": manifest["manifest_hash"],
                "git_commit": manifest["git_commit"], "file_count": len(entries),
            },
            "trusted_pins": pins.as_dict(),
            "native_source_overlay": {
                "reason": "raw native source bytes must equal the extension embedded_source_sha256",
                "files": overlay_files, "raw_manifest_hash": overlay_manifest["hash"],
            },
            "source_fingerprint": fingerprint,
            "native": {
                "filename": NATIVE_FILENAME, "bytes": native_bytes, "sha256": native_sha,
                "embedded_source_sha256": probe["embedded_source_sha256"],
                "compiler": probe["compiler"], "cxx_standard": int(probe["cxx_standard"]),
            },
            "deployment": runtime_block,
            "four_condition_probe": {
                "isolated_process": True, "executable": sys.executable,
                "source_matches_loaded_binary": probe["source_matches_loaded_binary"],
                "current_source_manifest_hash": probe["current_source_manifest_hash"],
                "extension_sha256": probe["extension_sha256"],
                "extension_filename": probe["extension_filename"],
            },
        }
        _write_json(staging / "runtime.json", runtime_json)
        _write_sums(staging, staging / "SHA256SUMS")
        _final_verify_staging(staging, runtime_json)
        _require(not target.exists(), f"target appeared during installation: {target}")
        staging.rename(target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    print(json.dumps({
        "runtime_id": runtime_id, "target": str(target),
        "source_fingerprint_hash": fingerprint["hash"],
        "raw_manifest_hash": overlay_manifest["hash"],
        "native_sha256": native_sha, "identity_sha256": identity.identity_sha256,
        "source_matches_loaded_binary": probe["source_matches_loaded_binary"],
        "trusted_pins": pins.as_dict(),
    }, indent=2))
    return 0


def _checkpoint_expectation(checkpoint: Path, model_version: str,
                            training_version: str) -> tuple[CheckpointExpectation, str]:
    data = checkpoint.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    payload = _restricted_load(data)
    variant = "flat_not_smoothed_v1" if "distributed_training" not in payload else "ddp_merged_frozen_v1"
    expectation = CheckpointExpectation(
        filename=checkpoint.name, bytes=len(data), sha256=digest,
        schema=payload["schema"], algorithm=payload["algorithm"],
        architecture=payload["architecture"], protocol_version=payload["protocol_version"],
        iteration=payload["iteration"], classification="evaluation_only",
        resume_classification=payload["resume_classification"],
        training_resume_allowed=payload["training_resume_allowed"],
        resolved_config_hash=payload["resolved_config_hash"], source_hash=payload["source_hash"],
        source_parent_commit=payload["source_parent_commit"],
        training_backend_identity_hash=payload["backend_identity"]["hash"],
        payload_variant=variant, model_version=model_version, training_version=training_version,
    ).validate()
    return expectation, digest


def build_bundle(args: argparse.Namespace) -> int:
    checkpoint = _lexical(args.checkpoint)
    runtime_dir = _lexical(args.runtime)
    output = _lexical(args.output)
    _reject_reparse_chain(checkpoint)
    _reject_reparse_chain(runtime_dir)
    _reject_reparse_chain(output)
    _require(checkpoint.is_file(), f"checkpoint missing: {checkpoint}")
    _require((runtime_dir / "runtime.json").is_file(), f"runtime.json missing: {runtime_dir}")
    _require(not output.exists(), f"output already exists: {output}")
    parent = output.parent
    _require(parent.is_dir(), f"output parent must already exist: {parent}")
    _reject_reparse_chain(parent)

    runtime = json.loads((runtime_dir / "runtime.json").read_text(encoding="utf-8"))
    if runtime.get("schema") != RUNTIME_SCHEMA:
        raise SystemExit("runtime.json schema mismatch")
    if runtime["four_condition_probe"]["source_matches_loaded_binary"] is not True:
        raise SystemExit("runtime probe is not positive")
    recomputed = _runtime_source_fingerprint(runtime_dir / "source")
    if recomputed["hash"] != runtime["source_fingerprint"]["hash"]:
        raise SystemExit("runtime source tree changed since installation")
    expectation, digest = _checkpoint_expectation(checkpoint, args.model_version,
                                                  args.training_version)
    deployment = build_deployment(checkpoint=expectation,
                                  source_fingerprint=runtime["source_fingerprint"],
                                  runtime=runtime["deployment"])
    if args.dry_run:
        print(json.dumps({"checkpoint_sha256": digest, "payload_variant": expectation.payload_variant,
                          "deployment_schema": deployment["schema"]}, indent=2))
        return 0

    staging = parent / f".{output.name}.staging-{uuid.uuid4().hex}"
    _require(not staging.exists(), "staging name collision")
    staging.mkdir()
    try:
        deployment_path = staging / "deployment.json"
        _write_json(deployment_path, deployment)
        deployment_sha, deployment_bytes = _sha256_file(deployment_path)
        manifest = provider_manifest("deployment.json", deployment_sha,
                                     model_version=args.model_version,
                                     training_version=args.training_version)
        manifest_data = manifest.to_dict()
        manifest_data.update({
            "schema_version": 2, "model_id": args.model_id, "runtime_contract": RUNTIME_CONTRACT,
            "classification": "evaluation_only",
            "readiness": {"experimental": True, "production_ready": False, "strength_claim": False},
            "artifact_bytes": deployment_bytes,
        })
        from guance.infrastructure.model.manifest import ModelManifest, load_bundle  # noqa: E402
        ModelManifest.from_dict(manifest_data)
        (staging / "SHA256SUMS").write_text(f"{deployment_sha}  deployment.json\n", encoding="utf-8")
        _write_json(staging / "manifest.json", manifest_data)
        bundle = load_bundle(staging / "manifest.json")
        descriptor = load_descriptor(bundle)
        _require(descriptor.bundle_fingerprint == deployment_sha, "bundle fingerprint mismatch")
        _require(not output.exists(), f"output appeared during build: {output}")
        staging.rename(output)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    print(json.dumps({
        "bundle": str(output), "model_id": args.model_id,
        "deployment_bytes": deployment_bytes, "deployment_sha256": deployment_sha,
        "checkpoint_sha256": digest, "payload_variant": expectation.payload_variant,
        "runtime_id": runtime["runtime_id"],
    }, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    install = sub.add_parser("install-runtime")
    install.add_argument("--source-zip", type=Path, required=True)
    install.add_argument("--source-manifest", type=Path, required=True)
    install.add_argument("--overlay-root", type=Path, required=True)
    install.add_argument("--native", type=Path, required=True)
    install.add_argument("--target", type=Path, required=True)
    install.add_argument("--expected-source-zip-sha256", required=True)
    install.add_argument("--expected-source-manifest-sha256", required=True)
    install.add_argument("--expected-source-content-manifest-hash", required=True)
    install.add_argument("--expected-source-commit", required=True)
    install.add_argument("--expected-native-sha256", required=True)
    install.add_argument("--expected-native-bytes", type=int, required=True)
    install.add_argument("--expected-overlay-manifest-hash", required=True)
    install.set_defaults(func=install_runtime)

    bundle = sub.add_parser("build-bundle")
    bundle.add_argument("--checkpoint", type=Path, required=True)
    bundle.add_argument("--runtime", type=Path, required=True)
    bundle.add_argument("--output", type=Path, required=True)
    bundle.add_argument("--model-id", required=True)
    bundle.add_argument("--model-version", required=True)
    bundle.add_argument("--training-version", required=True)
    bundle.add_argument("--dry-run", action="store_true")
    bundle.set_defaults(func=build_bundle)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
