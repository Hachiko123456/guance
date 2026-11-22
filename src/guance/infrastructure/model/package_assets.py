"""Resolve only package-internal MARVEL runtime assets when running frozen.

The frozen GPU package ships the already-accepted Torch/CUDA runtime, the frozen
MARVEL source/native tree, the real evaluation checkpoint and the deployment
descriptor under ``_internal/marvel_runtime``.  This module maps those trusted
package-relative locations to the existing strict environment contract used by
``deployment_bundle.resolve_environment``.  It never accepts paths from a model
bundle/manifest, never locates an interpreter or executable, and never falls
back to a development checkout.
"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import sys
from typing import TypedDict

MARVEL_ROOT_NAME = "marvel_runtime"
CHECKPOINT_FILENAME = "m09_candidate_iter002000.pt"
BUNDLE_DIR_NAME = "marvel-v2-iter002000-device"
SOURCE_ENV = "GUANCE_MARVEL_V2_SOURCE"
CHECKPOINT_ENV = "GUANCE_MARVEL_V2_CHECKPOINT"
DANKS_RUNTIME_DIR_NAME = "danks_runtime"
DANKS_BUNDLE_DIR_NAME = "danks-v3-top10-v1"
DANKS_RULES_DIR_NAME = "rules"


class PackagedMarvelPaths(TypedDict):
    runtime_root: Path
    source_root: Path
    checkpoint_path: Path
    bundle_dir: Path | None


def frozen_runtime_root() -> Path | None:
    """Return the trusted frozen package root, or ``None`` outside a frozen app."""
    if not getattr(sys, "frozen", False):
        return None
    base = getattr(sys, "_MEIPASS", None)
    if not isinstance(base, str) or not base:
        return None
    return Path(base) / MARVEL_ROOT_NAME


def packaged_marvel_paths(root: Path | None = None) -> PackagedMarvelPaths | None:
    """Return declared package assets if (and only if) they are all present."""
    runtime_root = Path(root) if root is not None else frozen_runtime_root()
    if runtime_root is None:
        return None
    source_root = runtime_root / "source"
    checkpoint_path = runtime_root / "checkpoint" / CHECKPOINT_FILENAME
    if not source_root.is_dir() or not checkpoint_path.is_file():
        return None
    bundle = runtime_root / "bundles" / BUNDLE_DIR_NAME
    return PackagedMarvelPaths(
        runtime_root=runtime_root,
        source_root=source_root,
        checkpoint_path=checkpoint_path,
        bundle_dir=bundle if bundle.is_dir() else None,
    )


def apply_packaged_marvel_defaults() -> bool:
    """Set the strict environment contract from package assets when unset.

    Explicitly configured values always win.  Returns ``True`` only when the
    frozen package actually declares both required assets.
    """
    paths = packaged_marvel_paths()
    if paths is None:
        return False
    os.environ.setdefault(SOURCE_ENV, str(paths["source_root"]))
    os.environ.setdefault(CHECKPOINT_ENV, str(paths["checkpoint_path"]))
    return True


def _validated_packaged_catalog_bundle(bundle: Path) -> list[Path]:
    """Return the strict file set of one packaged catalog bundle, or ``[]``.

    The checks mirror the runtime catalog discovery: manifest identity,
    artifact containment and hash, SHA256SUMS coverage, no extra files.  A
    bundle that fails any check is reported instead of copied.
    """
    import json as _json
    from hashlib import sha256 as _sha256

    manifest_path = bundle / "manifest.json"
    if not manifest_path.is_file():
        return []
    try:
        manifest = _json.loads(manifest_path.read_text(encoding="utf-8"))
        provider = manifest.get("provider")
        runtime = manifest.get("runtime")
        artifact = manifest.get("artifact")
        expected = {"danks_v3": "torch"}
        if expected.get(provider) != runtime or not isinstance(artifact, str):
            return []
        relative = PurePosixPath(artifact)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            return []
        artifact_path = (bundle.joinpath(*relative.parts)).resolve()
        if not artifact_path.is_relative_to(bundle.resolve()) or not artifact_path.is_file():
            return []
        if manifest.get("artifact_sha256") != _sha256(artifact_path.read_bytes()).hexdigest():
            return []
    except (OSError, UnicodeError, ValueError, TypeError, _json.JSONDecodeError):
        return []
    allowed = {manifest_path.resolve(), artifact_path}
    sums = bundle / "SHA256SUMS"
    if sums.is_file():
        seen: set[str] = set()
        try:
            for line in sums.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                parts = line.split()
                if len(parts) != 2 or len(parts[0]) != 64 or any(
                        c not in "0123456789abcdef" for c in parts[0]):
                    return []
                checksum_path = PurePosixPath(parts[1].lstrip("*"))
                if checksum_path.is_absolute() or any(
                        part in {"", ".", ".."} for part in checksum_path.parts):
                    return []
                key = checksum_path.as_posix()
                if key in seen:
                    return []
                seen.add(key)
                target = bundle.joinpath(*checksum_path.parts).resolve()
                if (not target.is_relative_to(bundle.resolve()) or not target.is_file()
                        or _sha256(target.read_bytes()).hexdigest() != parts[0]):
                    return []
            if str(relative) not in seen:
                return []
        except (OSError, UnicodeError):
            return []
        allowed.add(sums.resolve())
    extras = [path for path in bundle.rglob("*")
              if path.is_file() and path.resolve() not in allowed]
    if extras:
        return []
    return sorted(allowed)


def seed_packaged_catalog_bundles(paths: dict[str, Path]) -> dict[str, object]:
    """Copy packaged catalog bundles into the user's models catalog (frozen only).

    Only bundles that pass the strict packaged validation are seeded, only
    into an ABSENT target directory (an existing user bundle is never
    touched), and a partial copy is removed on any failure.  The report
    records every decision; a skipped bundle degrades to "not in catalog",
    never to unverified data.
    """
    base = Path(getattr(sys, "_MEIPASS", "")) if getattr(sys, "frozen", False) else None
    packaged_catalog = base / "data" / "models" / "catalog" if base is not None else None
    if packaged_catalog is None or not packaged_catalog.is_dir():
        return {"seeded": [], "skipped": {}, "frozen": False}
    target_catalog = paths.get("models_dir") / "catalog"
    seeded: list[str] = []
    skipped: dict[str, str] = {}
    for bundle in sorted(path for path in packaged_catalog.iterdir() if path.is_dir()):
        try:
            files = _validated_packaged_catalog_bundle(bundle)
        except OSError as exc:
            skipped[bundle.name] = f"validation error: {exc}"
            continue
        if not files:
            skipped[bundle.name] = "packaged bundle failed strict validation"
            continue
        target = target_catalog / bundle.name
        if target.exists():
            skipped[bundle.name] = "target already exists; user bundle left untouched"
            continue
        try:
            target.mkdir(parents=True)
            for source in files:
                destination = target / source.relative_to(bundle)
                destination.parent.mkdir(parents=True, exist_ok=True)
                _copy_verified(source, destination)
            for source in files:
                destination = target / source.relative_to(bundle)
                if source.stat().st_size != destination.stat().st_size or \
                        source.read_bytes() != destination.read_bytes():
                    raise OSError(f"copy verification failed: {destination}")
        except OSError as exc:
            skipped[bundle.name] = f"copy failed: {exc}"
            _remove_quietly(target)
            continue
        seeded.append(bundle.name)
    return {"seeded": seeded, "skipped": skipped, "frozen": True}


def _copy_verified(source: Path, destination: Path) -> None:
    import shutil
    shutil.copyfile(source, destination)


def _remove_quietly(target: Path) -> None:
    import shutil
    try:
        if target.exists():
            shutil.rmtree(target)
    except OSError:
        pass


__all__ = [
    "BUNDLE_DIR_NAME", "CHECKPOINT_ENV", "CHECKPOINT_FILENAME", "MARVEL_ROOT_NAME",
    "DANKS_BUNDLE_DIR_NAME", "DANKS_RULES_DIR_NAME", "DANKS_RUNTIME_DIR_NAME",
    "SOURCE_ENV", "PackagedMarvelPaths", "apply_packaged_marvel_defaults",
    "frozen_runtime_root", "packaged_marvel_paths", "seed_packaged_catalog_bundles",
]
