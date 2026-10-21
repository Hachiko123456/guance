"""Stable catalog bundle packaging tool for future same-contract model delivery.

Training/export delivery contract
---------------------------------
The training/export side hands over one directory containing exactly:

* ``manifest.json`` - catalog schema 2 manifest; ``artifact`` names the single
  artifact file in the same directory (one path segment, library name or
  ``deployment.json`` descriptor);
* the single artifact named by ``manifest.artifact``;
* optionally ``SHA256SUMS`` - if present it is verified, never trusted blindly.

No checkpoint bytes are copied into the bundle for MARVEL V2: the artifact is the
bounded deployment descriptor that pins the external checkpoint identity. The
tool verifies bytes/SHA-256 first, validates the bundle through the real provider
registry and (when supplied) the deployment descriptor against the real
checkpoint, and only then installs it.

Install protocol
----------------
The target is a dot-prefixed sibling staging directory inside the catalog root.
All files are written there and the bundle is re-validated with the production
``ModelBundle.verify``/registry/``load_descriptor`` gates before an atomic
rename to the final directory. Any failure removes only the tool's own staging
directory; an interrupted run can therefore never leave an activatable
half-package, and a pre-existing target or duplicate ``model_id`` is refused.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import uuid
from typing import Any

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT / "src"))
sys.dont_write_bytecode = True

from guance.infrastructure.model.catalog import scan_catalog  # noqa: E402
from guance.infrastructure.model.errors import ModelBackendError  # noqa: E402
from guance.infrastructure.model.manifest import (  # noqa: E402
    MAX_MANIFEST_BYTES, ModelBundle, ModelManifest, strict_json,
)
from guance.infrastructure.model.marvel_v2.deployment_bundle import load_descriptor  # noqa: E402
from guance.infrastructure.model.registry import ProviderRegistry, default_registry  # noqa: E402
from guance.infrastructure.model.selection import (  # noqa: E402
    is_reparse_point, safe_bundle_directory_name,
)

MANIFEST_NAME = "manifest.json"
CHECKSUMS_NAME = "SHA256SUMS"
MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
_ALLOWED_SOURCE_FILES = frozenset({MANIFEST_NAME, CHECKSUMS_NAME})
_HEX_DIGITS = set("0123456789abcdef")


class BundlePackagingError(RuntimeError):
    """Bounded packaging failure with a stable code."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


def _fail(code: str) -> None:
    raise BundlePackagingError(code)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                digest.update(chunk)
                size += len(chunk)
    except OSError:
        _fail("ARTIFACT_MISSING")
    return digest.hexdigest(), size


def _read_bounded(path: Path, maximum: int) -> bytes:
    try:
        raw = path.read_bytes()
    except OSError:
        _fail("ARTIFACT_MISSING")
    if len(raw) > maximum:
        _fail("MANIFEST_INVALID")
    return raw


def _is_hex64(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX_DIGITS


def _check_source_layout(source: Path, artifact_name: str) -> None:
    if "/" in artifact_name or "\\" in artifact_name:
        # Delivery contract: one artifact file directly inside the bundle.
        _fail("SOURCE_BUNDLE_INVALID")
    try:
        children = list(source.iterdir())
    except OSError:
        _fail("SOURCE_BUNDLE_INVALID")
    for child in children:
        if child.name.startswith("."):
            _fail("SOURCE_BUNDLE_EXTRA_FILE")
        if child.is_symlink() or is_reparse_point(child) or not child.is_file():
            _fail("SOURCE_BUNDLE_EXTRA_FILE")
        if child.name != artifact_name and child.name not in _ALLOWED_SOURCE_FILES:
            _fail("SOURCE_BUNDLE_EXTRA_FILE")


def _parse_source_checksums(raw: bytes, artifact_name: str, artifact_sha: str,
                            manifest_sha: str) -> None:
    entries: dict[str, str] = {}
    try:
        for line in raw.decode("utf-8").splitlines():
            if not line.strip():
                continue
            parts = line.split()
            if len(parts) != 2 or not _is_hex64(parts[0]):
                _fail("SOURCE_CHECKSUMS_INVALID")
            name = parts[1].lstrip("*")
            if "/" in name or "\\" in name or name in entries:
                _fail("SOURCE_CHECKSUMS_INVALID")
            entries[name] = parts[0]
    except UnicodeError:
        _fail("SOURCE_CHECKSUMS_INVALID")
    if entries.get(artifact_name) != artifact_sha:
        _fail("SOURCE_CHECKSUMS_INVALID")
    if MANIFEST_NAME in entries and entries[MANIFEST_NAME] != manifest_sha:
        _fail("SOURCE_CHECKSUMS_INVALID")


def _verify_checkpoint(descriptor: Any, checkpoint: Path) -> dict[str, Any]:
    actual_sha, actual_bytes = _sha256_file(checkpoint)
    block = descriptor.checkpoint
    if (checkpoint.name != block["filename"]
            or actual_sha != block["sha256"] or actual_bytes != block["bytes"]):
        _fail("CHECKPOINT_IDENTITY_MISMATCH")
    return {
        "filename": str(block["filename"]),
        "sha256": actual_sha,
        "bytes": actual_bytes,
        "iteration": int(block["iteration"]),
        "model_version": str(descriptor.checkpoint_expectation.model_version),
    }


def _resolve_bundle_name(bundle_name: str | None, manifest: ModelManifest) -> str:
    value = bundle_name if bundle_name is not None else manifest.model_id
    try:
        checked = safe_bundle_directory_name(value)
    except ModelBackendError:
        _fail("BUNDLE_NAME_INVALID")
    if checked != value:
        _fail("BUNDLE_NAME_INVALID")
    return checked


def _write_checksums(directory: Path, entries: dict[str, str]) -> bytes:
    text = "".join(f"{entries[name]}  {name}\n" for name in sorted(entries))
    raw = text.encode("utf-8")
    (directory / CHECKSUMS_NAME).write_bytes(raw)
    return raw


def _remove_owned(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()
    except OSError:
        pass


def _validation_record(registry: ProviderRegistry, manifest: ModelManifest,
                       directory: Path) -> tuple[ModelBundle, Any]:
    bundle = ModelBundle(ModelManifest.from_dict(manifest.to_dict()), directory)
    bundle.verify()
    registry.resolve_discovery(manifest)
    registry.resolve(manifest)
    descriptor = None
    if manifest.artifact_format == "guance_marvel_v2_deployment_v1":
        descriptor = load_descriptor(bundle)
    return bundle, descriptor


def package_bundle(source_bundle: Path | str, catalog_root: Path | str, *,
                   bundle_name: str | None = None, checkpoint: Path | str | None = None,
                   expected_manifest_sha256: str | None = None,
                   expected_artifact_sha256: str | None = None,
                   expected_artifact_bytes: int | None = None,
                   registry: ProviderRegistry | None = None) -> dict[str, Any]:
    """Verify and atomically install one bundle into a catalog. Returns a bounded record."""
    registry = registry if registry is not None else default_registry()
    source = Path(source_bundle)
    catalog = Path(catalog_root)
    if not source.is_dir() or source.is_symlink() or is_reparse_point(source):
        _fail("SOURCE_BUNDLE_INVALID")
    try:
        catalog.mkdir(parents=True, exist_ok=True)
    except OSError:
        _fail("CATALOG_INVALID")
    if not catalog.is_dir() or is_reparse_point(catalog):
        _fail("CATALOG_INVALID")

    # 1. Bytes/hash first: read the manifest and artifact and verify their identity.
    manifest_path = source / MANIFEST_NAME
    if not manifest_path.is_file():
        _fail("MANIFEST_MISSING")
    manifest_raw = _read_bounded(manifest_path, MAX_MANIFEST_BYTES)
    manifest_sha = _sha256_bytes(manifest_raw)
    try:
        manifest = ModelManifest.from_dict(strict_json(manifest_raw.decode("utf-8")))
    except ModelBackendError as exc:
        _fail(exc.code if exc.code in {"PROTOCOL_UNSUPPORTED"} else "MANIFEST_INVALID")
    except (UnicodeError, ValueError, RecursionError):
        _fail("MANIFEST_INVALID")
    if not manifest.is_catalog_manifest:
        _fail("MANIFEST_INVALID")
    if manifest.artifact_bytes is None or not 0 < manifest.artifact_bytes <= MAX_ARTIFACT_BYTES:
        _fail("ARTIFACT_SIZE_MISMATCH")

    _check_source_layout(source, manifest.artifact)
    artifact_path = source / manifest.artifact
    if not artifact_path.is_file() or artifact_path.is_symlink() or is_reparse_point(artifact_path):
        _fail("ARTIFACT_MISSING")
    artifact_sha, artifact_bytes = _sha256_file(artifact_path)
    if artifact_bytes != manifest.artifact_bytes:
        _fail("ARTIFACT_SIZE_MISMATCH")
    if artifact_sha != manifest.artifact_sha256:
        _fail("ARTIFACT_HASH_MISMATCH")
    if expected_manifest_sha256 is not None and manifest_sha != expected_manifest_sha256:
        _fail("EXPECTED_HASH_MISMATCH")
    if expected_artifact_sha256 is not None and artifact_sha != expected_artifact_sha256:
        _fail("EXPECTED_HASH_MISMATCH")
    if expected_artifact_bytes is not None and artifact_bytes != expected_artifact_bytes:
        _fail("EXPECTED_SIZE_MISMATCH")

    source_checksums = source / CHECKSUMS_NAME
    if source_checksums.exists():
        _parse_source_checksums(_read_bounded(source_checksums, MAX_MANIFEST_BYTES),
                                manifest.artifact, artifact_sha, manifest_sha)

    checkpoint_record = None
    checkpoint_path = Path(checkpoint) if checkpoint is not None else None
    base_bundle, base_descriptor = None, None
    if checkpoint_path is not None:
        if manifest.artifact_format != "guance_marvel_v2_deployment_v1":
            _fail("CHECKPOINT_UNSUPPORTED")
        base_bundle, base_descriptor = _validation_record(registry, manifest, source)
        checkpoint_record = _verify_checkpoint(base_descriptor, checkpoint_path)

    # 2. Catalog conflicts are checked before any staging write.
    target = catalog / _resolve_bundle_name(bundle_name, manifest)
    if target.exists():
        _fail("TARGET_EXISTS")
    scan = scan_catalog(catalog, registry=registry)
    if any(entry.model_id == manifest.model_id for entry in scan.entries):
        _fail("DUPLICATE_MODEL_ID")
    if (any(entry.directory == target.name for entry in scan.entries)
            or any(problem.directory == target.name for problem in scan.problems)):
        _fail("TARGET_EXISTS")

    # 3. Stage, validate with the production gates, atomically rename.
    staging = catalog / f".package-{uuid.uuid4().hex}.tmp"
    installed = False
    try:
        staging.mkdir()
        shutil.copyfile(artifact_path, staging / manifest.artifact)
        staged_sha, staged_bytes = _sha256_file(staging / manifest.artifact)
        if (staged_sha, staged_bytes) != (artifact_sha, artifact_bytes):
            _fail("ARTIFACT_HASH_MISMATCH")
        (staging / MANIFEST_NAME).write_bytes(manifest_raw)
        if _sha256_file(staging / MANIFEST_NAME)[0] != manifest_sha:
            _fail("MANIFEST_INVALID")
        checksums_raw = _write_checksums(staging, {
            manifest.artifact: artifact_sha, MANIFEST_NAME: manifest_sha})
        staged_bundle, staged_descriptor = _validation_record(registry, manifest, staging)
        if checkpoint_path is not None:
            if staged_descriptor is None:
                _fail("CHECKPOINT_UNSUPPORTED")
            _verify_checkpoint(staged_descriptor, checkpoint_path)
        if target.exists():
            _fail("TARGET_EXISTS")
        os.replace(staging, target)
        installed = True
        final_scan = scan_catalog(catalog, registry=registry)
        matches = [entry for entry in final_scan.entries
                   if entry.model_id == manifest.model_id and entry.directory == target.name]
        if len(matches) != 1 or any(problem.directory == target.name
                                    for problem in final_scan.problems):
            _fail("INSTALL_VERIFICATION_FAILED")
        final_entries = [
            {"model_id": entry.model_id, "directory": entry.directory,
             "provider": entry.provider, "runtime_contract": entry.runtime_contract}
            for entry in final_scan.entries]
    except BaseException:
        _remove_owned(staging)
        if installed:
            # The rename completed but the final catalog validation did not; the
            # tool owns this just-created target and must not leave a half-package.
            _remove_owned(target)
        raise

    # 4. Source must be untouched; the caller only received a new catalog entry.
    final_artifact_sha, final_artifact_bytes = _sha256_file(artifact_path)
    final_manifest_sha = _sha256_file(manifest_path)[0]
    if (final_artifact_sha, final_artifact_bytes, final_manifest_sha) != (
            artifact_sha, artifact_bytes, manifest_sha):
        _fail("SOURCE_MODIFIED")
    return {
        "schema": "guance-phase6-package-result-v1",
        "status": "installed",
        "model_id": manifest.model_id,
        "provider": manifest.provider,
        "bundle": target.name,
        "manifest_sha256": manifest_sha,
        "manifest_bytes": len(manifest_raw),
        "artifact": manifest.artifact,
        "artifact_sha256": artifact_sha,
        "artifact_bytes": artifact_bytes,
        "sha256sums_sha256": _sha256_bytes(checksums_raw),
        "checkpoint": checkpoint_record,
        "atomic": True,
        "source_unchanged": True,
        "catalog_entry_count": len(final_entries),
        "catalog_entries": final_entries,
    }


def _safe_reason(exc: BaseException) -> str:
    if isinstance(exc, BundlePackagingError):
        return exc.code
    value = str(exc)
    if value and len(value) <= 64 and all(ch.isupper() or ch.isdigit() or ch == "_" for ch in value):
        return value
    return "PACKAGE_FAILED"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-bundle", type=Path, required=True)
    parser.add_argument("--catalog-root", type=Path, required=True)
    parser.add_argument("--bundle-name", default=None)
    parser.add_argument("--checkpoint", type=Path, default=None)
    parser.add_argument("--expected-manifest-sha256", default=None)
    parser.add_argument("--expected-artifact-sha256", default=None)
    parser.add_argument("--expected-artifact-bytes", type=int, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    try:
        result = package_bundle(
            args.source_bundle, args.catalog_root, bundle_name=args.bundle_name,
            checkpoint=args.checkpoint,
            expected_manifest_sha256=args.expected_manifest_sha256,
            expected_artifact_sha256=args.expected_artifact_sha256,
            expected_artifact_bytes=args.expected_artifact_bytes)
    except Exception as exc:  # bounded fail-closed result
        payload = {"schema": "guance-phase6-package-result-v1", "status": "failed",
                   "reason": _safe_reason(exc), "atomic": False}
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        print(text)
        if args.output is not None:
            args.output.write_text(text + "\n", encoding="utf-8")
        return 2
    text = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    print(text)
    if args.output is not None:
        args.output.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
