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
from pathlib import Path
import sys
from typing import TypedDict

MARVEL_ROOT_NAME = "marvel_runtime"
CHECKPOINT_FILENAME = "m09_candidate_iter002000.pt"
BUNDLE_DIR_NAME = "marvel-v2-iter002000-device"
SOURCE_ENV = "GUANCE_MARVEL_V2_SOURCE"
CHECKPOINT_ENV = "GUANCE_MARVEL_V2_CHECKPOINT"


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


__all__ = [
    "BUNDLE_DIR_NAME", "CHECKPOINT_ENV", "CHECKPOINT_FILENAME", "MARVEL_ROOT_NAME",
    "SOURCE_ENV", "PackagedMarvelPaths", "apply_packaged_marvel_defaults",
    "frozen_runtime_root", "packaged_marvel_paths",
]
