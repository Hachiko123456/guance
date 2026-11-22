"""Source identity for the vendored DanKS V3 reference subset.

The deployment must never depend on the user's DanKS-Train checkout path;
the vendored tree under ``vendor/`` is the only runtime source of engine
rules, retrieval, encoding and network code.  ``vendor/vendor_manifest.json``
records, per file, the original DanKS-Train path, the frozen reference
commit and the SHA-256 of the pristine original bytes, so the provenance of
every vendored line stays auditable.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
VENDOR_ROOT = PACKAGE_ROOT / "vendor"
VENDOR_MANIFEST_PATH = VENDOR_ROOT / "vendor_manifest.json"

# Frozen reference checkout (verified before vendoring; see the phase 1
# evidence inputs.json for the per-file hashes of the same commit).
REFERENCE_COMMIT = "c618f537c7a83c77b76c3082985e42efff67ec02"

# The checkpoint this provider is built for (phase 1 frozen identity).
CHECKPOINT_SHA256 = "91356edfd46ccfd54d03d5ce8bf181d3b31326d5650897a5a3d0c29e1bcedb08"
CHECKPOINT_BYTES = 18151074


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest() -> dict:
    if not VENDOR_MANIFEST_PATH.is_file():
        raise RuntimeError("danks_v3 vendor manifest is missing")
    return json.loads(VENDOR_MANIFEST_PATH.read_text(encoding="utf-8"))


def verify_vendor_manifest() -> dict[str, int]:
    """Recompute vendored file hashes against the manifest.

    Returns counts; raises RuntimeError on any mismatch or missing file so a
    tampered/partial vendor tree cannot load silently.
    """
    manifest = load_manifest()
    if manifest.get("reference_commit") != REFERENCE_COMMIT:
        raise RuntimeError("danks_v3 vendor manifest pins a different commit")
    verified = 0
    for entry in manifest["files"]:
        path = _vendored_path(entry["vendored_path"])
        if not path.is_file():
            raise RuntimeError(f"vendored file missing: {entry['vendored_path']}")
        if _sha256_file(path) != entry["vendored_sha256"]:
            raise RuntimeError(
                f"vendored file hash mismatch: {entry['vendored_path']}")
        verified += 1
    return {"files": verified, "manifest_entries": len(manifest["files"])}


def _vendored_path(repo_relative: str) -> Path:
    """Resolve a manifest path recorded relative to the repository root.

    Package layout: src/guance/infrastructure/model/danks_v3/ -> repo root is
    four levels up from PACKAGE_ROOT's parent chain (model, infrastructure,
    guance, src).
    """
    repo_root = PACKAGE_ROOT.parents[3].parent
    return repo_root / repo_relative
