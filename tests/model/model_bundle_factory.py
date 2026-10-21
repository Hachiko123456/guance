"""Synthetic catalog bundle fixtures for discovery/selection tests.

Artifacts are tiny placeholder bytes: Phase 1 only claims discovery/validation/
selection, so no real weights (and no NumPy) are required or imported.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

FABLEDAN_ARTIFACT_NAME = "artifact.npz"
MARVEL_ARTIFACT_NAME = "deployment.json"


def base_v2_manifest(model_id: str, provider: str = "fabledan") -> dict:
    if provider == "fabledan":
        manifest = {
            "schema_version": 2, "model_id": model_id, "provider": provider,
            "backend": "numpy", "runtime": "numpy", "runtime_contract": "fabledan.numpy.v1",
            "artifact": FABLEDAN_ARTIFACT_NAME, "artifact_format": "npz",
            "input_contract": "CanonicalPublicGameView.v1",
            "output_contract": "CanonicalAdviceResult.v1",
            "model_version": "legacy", "training_version": "legacy-unknown",
            "rules_version": "tencent_daguandan.v1", "action_version": "public-action.v1",
            "environment_version": "public-play.v1", "encoding_version": "fabledan.v1",
            "algorithm": "fabledan-q", "architecture": "FableDanNet",
            "classification": "legacy",
            "readiness": {"experimental": False, "production_ready": False, "strength_claim": False},
            "capabilities": {"recommend": True, "top_n": True},
        }
    elif provider == "guandan_marvel_v2":
        manifest = {
            "schema_version": 2, "model_id": model_id, "provider": provider,
            "backend": "guandan", "runtime": "torch", "runtime_contract": "marvel_v2.cpu.v1",
            "artifact": MARVEL_ARTIFACT_NAME,
            "artifact_format": "guance_marvel_v2_deployment_v1",
            "input_contract": "GuandanMARVELV2.Rollout.v1",
            "output_contract": "GuandanMARVELV2.ActionRow.v2",
            "model_version": "MARVEL-V2-v1.3-iter000100",
            "training_version": "M09-IPPO-pilot-20261011",
            "rules_version": "GD-RULES-0.1", "action_version": "GD-ACTION-0.2-MARVEL",
            "environment_version": "GD-ENV-0.2-MARVEL", "encoding_version": "GD-ENCODING-0.2-MARVEL",
            "algorithm": "ippo", "architecture": "v1.3",
            "classification": "evaluation_only",
            "readiness": {"experimental": True, "production_ready": False, "strength_claim": False},
            "capabilities": {"recommend": True, "next_token_policy": True},
        }
    elif provider == "guandan":
        manifest = {
            "schema_version": 2, "model_id": model_id, "provider": provider,
            "backend": "guandan", "runtime": "unavailable",
            "runtime_contract": "guandan.runtime.v1",
            "artifact": "artifact.pt", "artifact_format": "torch_checkpoint",
            "input_contract": "CanonicalPublicGameView.v1",
            "output_contract": "CanonicalAdviceResult.v1",
            "model_version": "0.1-scaffold", "training_version": "test-only",
            "rules_version": "GD-RULES-0.1", "action_version": "GD-ACTION-0.1",
            "environment_version": "GD-ENV-0.1", "encoding_version": "GD-ENCODING-0.1",
            "algorithm": "guandan-token", "architecture": "scaffold",
            "classification": "experimental",
            "readiness": {"experimental": True, "production_ready": False, "strength_claim": False},
            "capabilities": {"recommend": True},
        }
    else:
        raise ValueError(f"no base manifest template for {provider!r}")
    return manifest


def write_bundle(directory: Path, *, model_id: str, provider: str = "fabledan",
                 artifact: bytes = b"synthetic-artifact",
                 artifact_name: str | None = None,
                 manifest_overrides: dict | None = None,
                 manifest_drops: tuple[str, ...] = (),
                 write_checksums: bool = True,
                 checksum_text: str | None = None,
                 write_manifest: bool = True) -> Path:
    """Write one complete bundle. artifact hash/bytes always match the written file."""
    directory.mkdir(parents=True, exist_ok=True)
    manifest = base_v2_manifest(model_id, provider)
    name = artifact_name or str(manifest["artifact"])
    manifest["artifact"] = name
    (directory / name).write_bytes(artifact)
    digest = hashlib.sha256(artifact).hexdigest()
    manifest["artifact_sha256"] = digest
    manifest["artifact_bytes"] = len(artifact)
    if manifest_overrides:
        manifest.update(manifest_overrides)
    for key in manifest_drops:
        manifest.pop(key, None)
    if write_manifest:
        (directory / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True), encoding="utf-8")
    if write_checksums:
        text = checksum_text if checksum_text is not None else f"{digest}  {name}\n"
        (directory / "SHA256SUMS").write_text(text, encoding="utf-8")
    return directory


def manifest_path_of(directory: Path) -> Path:
    return directory / "manifest.json"


__all__ = ["FABLEDAN_ARTIFACT_NAME", "MARVEL_ARTIFACT_NAME", "base_v2_manifest",
           "manifest_path_of", "write_bundle"]
