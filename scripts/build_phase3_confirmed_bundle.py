#!/usr/bin/env python3
"""Build the Phase 3 confirmed-decision bundle from frozen Phase 2 materials.

The deployment descriptor is byte-copied from the Phase 2 iter2000 bundle (its
identity is re-verified against the Phase 2 runtime record); only the manifest
gains the implemented ``complete_action`` + ``confirmed_decision`` capabilities.
Nothing under the Phase 2 evidence root is modified. Assembly is atomic through
a sibling staging directory and the result is validated by the production
``load_bundle``/``load_descriptor`` gates before the rename.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from guance.infrastructure.model.manifest import ModelManifest, load_bundle  # noqa: E402
from guance.infrastructure.model.marvel_v2.deployment_bundle import (  # noqa: E402
    load_descriptor, provider_manifest,
)
from guance.infrastructure.model.marvel_v2.identity import CONTRACT_FIELDS  # noqa: E402

RUNTIME_SCHEMA = "guance-marvel-v2-runtime-v1"
RUNTIME_CONTRACT = "marvel_v2.cpu.v1"


def _fail(message: str) -> None:
    raise SystemExit(f"phase3-bundle: {message}")


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase2-bundle", type=Path, required=True)
    parser.add_argument("--phase2-runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-id", required=True)
    args = parser.parse_args()

    bundle_dir = args.phase2_bundle.resolve()
    runtime_dir = args.phase2_runtime.resolve()
    output = args.output.resolve()
    if output.exists():
        _fail(f"output already exists: {output}")
    phase2_manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    deployment = bundle_dir / "deployment.json"
    deployment_sha, deployment_bytes = _sha256(deployment)
    if (deployment_sha != phase2_manifest["artifact_sha256"]
            or deployment_bytes != phase2_manifest["artifact_bytes"]):
        _fail("phase 2 deployment identity mismatch")
    descriptor = json.loads(deployment.read_text(encoding="utf-8"))
    runtime_record = json.loads((runtime_dir / "runtime.json").read_text(encoding="utf-8"))
    if runtime_record.get("schema") != RUNTIME_SCHEMA:
        _fail("phase 2 runtime schema mismatch")
    if runtime_record["four_condition_probe"]["source_matches_loaded_binary"] is not True:
        _fail("phase 2 runtime probe is not positive")
    if (descriptor["source_fingerprint"]["hash"]
            != runtime_record["source_fingerprint"]["hash"]):
        _fail("descriptor source fingerprint does not match the Phase 2 runtime record")
    if descriptor["runtime"]["identity_sha256"] != runtime_record["deployment"]["identity_sha256"]:
        _fail("descriptor deployment identity does not match the Phase 2 runtime record")
    if descriptor["runtime"]["native_sha256"] != runtime_record["native"]["sha256"]:
        _fail("descriptor native identity does not match the Phase 2 runtime record")

    manifest = provider_manifest(
        "deployment.json", deployment_sha,
        model_version=phase2_manifest["model_version"],
        training_version=phase2_manifest["training_version"])
    manifest_data = manifest.to_dict()
    manifest_data.update({
        "schema_version": 2, "model_id": args.model_id,
        "runtime_contract": RUNTIME_CONTRACT, "classification": "evaluation_only",
        "readiness": {"experimental": True, "production_ready": False, "strength_claim": False},
        "artifact_bytes": deployment_bytes,
        "capabilities": {"recommend": True, "next_token_policy": True,
                         "complete_action": True, "confirmed_decision": True},
    })
    ModelManifest.from_dict(manifest_data)

    staging = output.parent / f".{output.name}.staging-{uuid.uuid4().hex}"
    if staging.exists():
        _fail("staging name collision")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging.mkdir()
    try:
        shutil.copyfile(deployment, staging / "deployment.json")
        copied_sha, copied_bytes = _sha256(staging / "deployment.json")
        if (copied_sha, copied_bytes) != (deployment_sha, deployment_bytes):
            _fail("staged deployment copy mismatch")
        (staging / "SHA256SUMS").write_text(
            f"{deployment_sha}  deployment.json\n", encoding="utf-8")
        (staging / "manifest.json").write_text(
            json.dumps(manifest_data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8")
        bundle = load_bundle(staging / "manifest.json")
        descriptor_checked = load_descriptor(bundle)
        if descriptor_checked.bundle_fingerprint != deployment_sha:
            _fail("bundle fingerprint mismatch after validation")
        if output.exists():
            _fail(f"output appeared during build: {output}")
        staging.rename(output)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    print(json.dumps({
        "bundle": str(output), "model_id": args.model_id,
        "deployment_sha256": deployment_sha, "deployment_bytes": deployment_bytes,
        "checkpoint_sha256": descriptor["checkpoint"]["sha256"],
        "runtime_identity_sha256": descriptor["runtime"]["identity_sha256"],
        "capabilities": manifest_data["capabilities"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
