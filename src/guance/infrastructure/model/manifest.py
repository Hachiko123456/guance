"""Strict local model bundles and the explicit legacy .npz compatibility wrapper."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from types import MappingProxyType
from typing import Any, TYPE_CHECKING

from guance.contracts import ADVICE_CONTRACT, MANIFEST_SCHEMA_VERSION, PUBLIC_GAME_CONTRACT, PUBLIC_RULESET
from .errors import ModelBackendError

if TYPE_CHECKING:
    from .registry import ProviderRegistry

PROTOCOL_VERSION = 1  # Manifest compatibility version; worker protocol has its own version.
MAX_MANIFEST_BYTES = 64 * 1024
CAPABILITY_NAMES = frozenset({"recommend", "top_n", "complete_action", "next_token_policy",
                              "beam_search", "belief", "search", "public_plus_self"})
_REQUIRED = frozenset({
    "schema_version", "backend", "provider", "artifact", "artifact_format", "input_contract",
    "output_contract", "model_version", "training_version", "rules_version", "action_version",
    "environment_version", "encoding_version", "algorithm", "architecture", "runtime",
    "artifact_sha256", "capabilities",
})
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate JSON member")
        obj[key] = value
    return obj


def strict_json(text: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError("non-finite JSON constant")
    return json.loads(text, object_pairs_hook=_object, parse_constant=reject_constant)


def sha256_file(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        raise ModelBackendError("ARTIFACT_MISSING") from None


@dataclass(frozen=True, slots=True)
class ModelManifest:
    schema_version: int
    backend: str
    provider: str
    artifact: str
    artifact_format: str
    input_contract: str
    output_contract: str
    model_version: str
    training_version: str
    rules_version: str
    action_version: str
    environment_version: str
    encoding_version: str
    algorithm: str
    architecture: str
    runtime: str
    artifact_sha256: str
    capabilities: Mapping[str, bool] | tuple[str, ...]
    protocol_version: int = PROTOCOL_VERSION

    @classmethod
    def from_dict(cls, data: Any) -> ModelManifest:
        if (not isinstance(data, dict) or not _REQUIRED <= data.keys()
                or data.keys() - (_REQUIRED | {"protocol_version"})):
            raise ModelBackendError("MANIFEST_INVALID")
        if type(data["schema_version"]) is not int or data["schema_version"] != MANIFEST_SCHEMA_VERSION:
            raise ModelBackendError("MANIFEST_INVALID")
        protocol = data.get("protocol_version", PROTOCOL_VERSION)
        if type(protocol) is not int or protocol != PROTOCOL_VERSION:
            raise ModelBackendError("PROTOCOL_UNSUPPORTED")
        for name in _REQUIRED - {"schema_version", "artifact", "artifact_sha256", "capabilities"}:
            if not isinstance(data[name], str) or not _IDENTIFIER.fullmatch(data[name]):
                raise ModelBackendError("MANIFEST_INVALID")
        # Contract identifiers are syntactic here. The selected provider declares
        # compatibility; a future registered ACL need not use the same input identifier.
        digest = data["artifact_sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ModelBackendError("MANIFEST_INVALID")
        caps = data["capabilities"]
        if isinstance(caps, Mapping):
            if (not caps or len(caps) > 32 or any(key not in CAPABILITY_NAMES for key in caps)
                    or any(type(value) is not bool for value in caps.values())
                    or caps.get("recommend") is not True):
                raise ModelBackendError("MANIFEST_INVALID")
            normalized_caps: Mapping[str, bool] | tuple[str, ...] = MappingProxyType(dict(caps))
        elif isinstance(caps, (list, tuple)):
            # Explicit compatibility with early bundles, without requiring a visibility capability.
            if (not caps or len(caps) > 32
                    or not all(isinstance(c, str) and c in CAPABILITY_NAMES for c in caps)
                    or len(set(caps)) != len(caps) or "recommend" not in caps):
                raise ModelBackendError("MANIFEST_INVALID")
            normalized_caps = tuple(caps)
        else:
            raise ModelBackendError("MANIFEST_INVALID")
        _relative_artifact(data["artifact"])
        return cls(**{**data, "capabilities": normalized_caps, "protocol_version": protocol})

    def to_dict(self) -> dict[str, Any]:
        result = {field.name: getattr(self, field.name) for field in fields(self)}
        result["capabilities"] = (dict(sorted(self.capabilities.items()))
                                  if isinstance(self.capabilities, Mapping) else list(self.capabilities))
        return result


def _relative_artifact(value: Any) -> PurePosixPath:
    if (not isinstance(value, str) or not value or len(value) > 512
            or "\\" in value or any(ord(c) < 32 for c in value) or ":" in value
            or any(c in value for c in '<>"|?*')):
        raise ModelBackendError("ARTIFACT_PATH_INVALID")
    path = PurePosixPath(value)
    if (path.is_absolute() or PureWindowsPath(value).drive
            or any(p in ("", ".", "..") or p.endswith((" ", "."))
                   or PureWindowsPath(p).is_reserved() for p in value.split("/"))):
        raise ModelBackendError("ARTIFACT_PATH_INVALID")
    return path


@dataclass(frozen=True, slots=True)
class ModelBundle:
    manifest: ModelManifest
    root: Path
    legacy: bool = False

    @property
    def artifact_path(self) -> Path:
        root = self.root.resolve()
        candidate = root.joinpath(*_relative_artifact(self.manifest.artifact).parts).resolve()
        if not candidate.is_relative_to(root):
            raise ModelBackendError("ARTIFACT_PATH_INVALID")
        return candidate

    def verify(self) -> None:
        # Recheck even for directly constructed descriptors and before every runtime load.
        ModelManifest.from_dict(self.manifest.to_dict())
        artifact = self.artifact_path
        if not artifact.is_file():
            raise ModelBackendError("ARTIFACT_MISSING")
        digest = sha256_file(artifact)
        if digest != self.manifest.artifact_sha256:
            raise ModelBackendError("ARTIFACT_HASH_MISMATCH")
        checksums = self.root.resolve() / "SHA256SUMS"
        if checksums.exists():
            if not checksums.is_file():
                raise ModelBackendError("SHA256SUMS_INVALID")
            try:
                raw = checksums.read_bytes()
                if len(raw) > MAX_MANIFEST_BYTES:
                    raise ValueError
                entries: dict[str, str] = {}
                for line in raw.decode("utf-8").splitlines():
                    if not line.strip():
                        continue
                    parts = line.split()
                    if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{64}", parts[0]):
                        raise ValueError
                    checksum_path = _relative_artifact(parts[1].lstrip("*"))
                    key = str(checksum_path)
                    if key in entries:
                        raise ValueError
                    entries[key] = parts[0]
                key = str(_relative_artifact(self.manifest.artifact))
                if entries.get(key) != digest:
                    raise ValueError
            except (OSError, UnicodeError, ValueError, RecursionError):
                raise ModelBackendError("SHA256SUMS_INVALID") from None


def legacy_npz_bundle(source: Path | str) -> ModelBundle:
    path = Path(source).resolve()
    if path.suffix.lower() != ".npz":
        raise ModelBackendError("MODEL_SOURCE_INVALID")
    if not path.is_file():
        raise ModelBackendError("ARTIFACT_MISSING")
    manifest = ModelManifest.from_dict({
        "schema_version": MANIFEST_SCHEMA_VERSION, "backend": "numpy", "provider": "fabledan",
        "artifact": path.name, "artifact_format": "npz", "runtime": "numpy",
        "input_contract": PUBLIC_GAME_CONTRACT, "output_contract": ADVICE_CONTRACT,
        "model_version": "legacy", "training_version": "legacy-unknown",
        "rules_version": PUBLIC_RULESET, "action_version": "public-action.v1",
        "environment_version": "public-play.v1", "encoding_version": "fabledan.v1",
        "algorithm": "fabledan-q", "architecture": "FableDanNet",
        "artifact_sha256": sha256_file(path),
        "capabilities": {"recommend": True, "top_n": True, "complete_action": True,
                         "next_token_policy": False, "beam_search": False, "belief": False, "search": False},
    })
    return ModelBundle(manifest, path.parent, legacy=True)


def load_bundle(source: Path | str | ModelBundle, *,
                registry: ProviderRegistry | None = None) -> ModelBundle:
    # Supported backends/runtimes are declared by the selected registry, not a second
    # hard-coded table. Explicitly injected registries can add provider families.
    if registry is None:
        from .registry import default_registry
        registry = default_registry()
    if isinstance(source, ModelBundle):
        source.verify()
        registry.resolve(source.manifest)
        return source
    if not isinstance(source, (Path, str)) or not str(source).strip():
        raise ModelBackendError("MODEL_SOURCE_INVALID")
    path = Path(source).resolve()
    if path.suffix.lower() == ".npz":
        bundle = legacy_npz_bundle(path)
    else:
        manifest_path = path / "manifest.json" if path.is_dir() else path
        if manifest_path.suffix.lower() != ".json":
            raise ModelBackendError("MODEL_SOURCE_INVALID")
        if not manifest_path.is_file():
            raise ModelBackendError("MANIFEST_MISSING")
        try:
            with manifest_path.open("rb") as stream:
                raw = stream.read(MAX_MANIFEST_BYTES + 1)
            if len(raw) > MAX_MANIFEST_BYTES:
                raise ValueError("oversized")
            manifest = ModelManifest.from_dict(strict_json(raw.decode("utf-8")))
        except (OSError, UnicodeError, ValueError, RecursionError) as exc:
            if isinstance(exc, ModelBackendError):
                raise
            raise ModelBackendError("MANIFEST_INVALID") from None
        bundle = ModelBundle(manifest, manifest_path.parent)
    bundle.verify()
    registry.resolve(bundle.manifest)
    return bundle
