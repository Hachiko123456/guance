"""Bounded Windows deployment identity helpers for the Phase 5 parity gate.

This module records runtime/native identity metadata only. It never loads a
checkpoint, imports Torch eagerly, or claims that two platform binaries are
byte-identical.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import platform
import struct
from pathlib import Path
import sys
from typing import Iterable, Mapping
import json

from .lifecycle_contracts import digest_fields, fail, sha256_hex

TRAINING_BACKEND_SHA256 = "5b8b7300f8bb32f0892959990ff9414dce67b51bd371e1063a8d9e78894c30de"
TRAINING_NATIVE_SHA256 = "5b9a1fd71361c27cdecafe8d6612165e8cfbdddd948b47679cfb6956f6e17a83"
TRAINING_SOURCE_SHA256 = "2c5a26c8ca8b645f22caa40f2ec25c7bf94555d83e06d4d6fb4bc843cb275b60"
TRAINING_PYTHON_TAG = "cpython-313"
LOCAL_NATIVE_SOURCE_SHA256 = "40058234af8227e9f0a0aa8c555959717e277f4ac16ed5094a918a32d46f6898"
LOCAL_NATIVE_SHA256 = "55803483a3271ddfc53a04930ad7b40c13a8ce7da6d02981a426f4b08602a9dd"


def file_sha256(path: str | Path) -> tuple[str, int]:
    p = Path(path)
    try:
        h = sha256(); size = 0
        with p.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                size += len(chunk); h.update(chunk)
        return h.hexdigest(), size
    except OSError:
        fail("deployment_binary_unavailable")


def source_manifest_identity(source_root: str | Path, relative_paths: Iterable[str], *,
                             normalize_newlines: bool = False) -> tuple[str, tuple[Mapping[str, str | int], ...]]:
    root = Path(source_root)
    entries = []
    for relative in relative_paths:
        if not isinstance(relative, str) or not relative or ".." in Path(relative).parts:
            fail("deployment_source_path_invalid")
        path = root / relative
        try:
            raw = path.read_bytes()
        except OSError:
            fail("deployment_source_unavailable")
        if normalize_newlines:
            raw = raw.replace(b"\r\n", b"\n")
        entries.append({"path": relative.replace("\\", "/"), "bytes": len(raw),
                        "sha256": sha256(raw).hexdigest()})
    payload = {"schema": "guandan-native-build-identity-v1", "files": entries}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return sha256(encoded).hexdigest(), tuple(entries)


@dataclass(frozen=True, slots=True)
class DeploymentIdentity:
    os_name: str
    os_release: str
    python_implementation: str
    python_version: str
    python_cache_tag: str
    pointer_bits: int
    torch_version: str
    torch_cuda: str
    native_path: str
    native_sha256: str
    native_bytes: int
    native_source_sha256: str
    compiler: str
    cxx_standard: int = 202002
    backend: str = "cpp"
    protocol_version: str = "GD-ACTION-0.2-MARVEL|GD-ENV-0.2-MARVEL|GD-ENCODING-0.2-MARVEL"

    def validate(self) -> None:
        if self.backend != "cpp" or self.cxx_standard != 202002:
            fail("deployment_backend_invalid")
        if self.pointer_bits != 64 or self.python_implementation != "CPython":
            fail("deployment_python_invalid")
        if not self.python_version or not self.python_cache_tag:
            fail("deployment_python_identity_invalid")
        sha256_hex(self.native_sha256, "deployment_native_hash_invalid")
        sha256_hex(self.native_source_sha256, "deployment_source_hash_invalid")
        if type(self.native_bytes) is not int or self.native_bytes <= 0:
            fail("deployment_native_bytes_invalid")

    @property
    def identity_sha256(self) -> str:
        self.validate()
        return digest_fields("guance-gmv2-windows-deployment-identity-v1", (
            ("os", self.os_name), ("release", self.os_release),
            ("python", self.python_version), ("cache_tag", self.python_cache_tag),
            ("torch", self.torch_version), ("cuda", self.torch_cuda),
            ("native", self.native_sha256), ("native_bytes", self.native_bytes),
            ("source", self.native_source_sha256), ("compiler", self.compiler),
            ("cxx", self.cxx_standard), ("protocol", self.protocol_version),
        ))

    def as_dict(self) -> Mapping[str, str | int]:
        self.validate()
        return {
            "os_name": self.os_name, "os_release": self.os_release,
            "python_implementation": self.python_implementation,
            "python_version": self.python_version, "python_cache_tag": self.python_cache_tag,
            "pointer_bits": self.pointer_bits, "torch_version": self.torch_version,
            "torch_cuda": self.torch_cuda, "native_path": self.native_path,
            "native_sha256": self.native_sha256, "native_bytes": self.native_bytes,
            "native_source_sha256": self.native_source_sha256, "compiler": self.compiler,
            "cxx_standard": self.cxx_standard, "backend": self.backend,
            "protocol_version": self.protocol_version, "identity_sha256": self.identity_sha256,
        }


def collect_deployment_identity(native_path: str | Path, *, torch_version: str,
                                torch_cuda: str | None, native_source_sha256: str,
                                compiler: str) -> DeploymentIdentity:
    native_hash, native_bytes = file_sha256(native_path)
    result = DeploymentIdentity(
        os_name=platform.system(), os_release=platform.release(),
        python_implementation=platform.python_implementation(),
        python_version=platform.python_version(),
        python_cache_tag=sys.implementation.cache_tag or "",
        pointer_bits=struct.calcsize("P") * 8,
        torch_version=str(torch_version), torch_cuda=str(torch_cuda or "cpu"),
        native_path=str(Path(native_path)), native_sha256=native_hash,
        native_bytes=native_bytes, native_source_sha256=native_source_sha256,
        compiler=str(compiler),
    )
    result.validate(); return result


def require_platform_distinct_from_training(identity: DeploymentIdentity) -> None:
    identity.validate()
    if identity.native_sha256 == TRAINING_NATIVE_SHA256:
        fail("deployment_native_identity_not_distinct")
    if identity.python_cache_tag == TRAINING_PYTHON_TAG:
        fail("deployment_python_identity_not_distinct")
    if identity.native_source_sha256 != LOCAL_NATIVE_SOURCE_SHA256:
        fail("deployment_source_identity_mismatch")


__all__ = [
    "DeploymentIdentity", "LOCAL_NATIVE_SHA256", "LOCAL_NATIVE_SOURCE_SHA256",
    "TRAINING_BACKEND_SHA256", "TRAINING_NATIVE_SHA256", "TRAINING_PYTHON_TAG",
    "TRAINING_SOURCE_SHA256", "collect_deployment_identity", "file_sha256",
    "require_platform_distinct_from_training", "source_manifest_identity",
]
