"""Build task-local MARVEL V2 device-contract bundles for the GPU-first task.

Only descriptors/manifests are written. Checkpoint bytes, the frozen Phase 2
runtime source and Phase 1-5 evidence are read-only inputs. The GPU runtime
block describes the exact isolated environment used for this task; the
checkpoint expectation and source fingerprint are copied from the verified
Phase 2 descriptor, so model identity and weights do not change with device.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import struct
import sys
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT / "src"))
sys.dont_write_bytecode = True

from guance.infrastructure.model.marvel_v2.checkpoint import CheckpointExpectation  # noqa: E402
from guance.infrastructure.model.marvel_v2.deployment import DeploymentIdentity  # noqa: E402
from guance.infrastructure.model.marvel_v2.deployment_bundle import build_deployment  # noqa: E402

PHASE2 = CODE_ROOT / "build" / "acceptance" / "guance-model-platform-20261016" / "phase2"
DEFAULT_BUNDLES = {
    "marvel-v2-iter000100": (
        PHASE2 / "bundles" / "marvel-v2-iter000100",
        Path(r"D:\project\guandan\runs\comparison_delivery\dataset\m09_champion_iter000100.pt"),
    ),
    "marvel-v2-iter000500": (
        PHASE2 / "bundles" / "marvel-v2-iter000500",
        Path(r"D:\project\guandan\runs\M09_full_training\remote"
             r"\iter500_formal_eval_20261014T030527Z\extracted\candidates\ippo_iter000500.pt"),
    ),
    "marvel-v2-iter002000": (
        PHASE2 / "bundles" / "marvel-v2-iter002000",
        Path(r"D:\project\guandan\runs\comparison_delivery\dataset\m09_candidate_iter002000.pt"),
    ),
}
DEVICE_CONTRACT = "marvel_v2.device.v1"


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _runtime_block(source_runtime: dict, *, torch_version: str, torch_cuda: str) -> dict:
    identity = DeploymentIdentity(
        os_name=platform.system(),
        os_release=platform.release(),
        python_implementation=platform.python_implementation(),
        python_version=platform.python_version(),
        python_cache_tag=sys.implementation.cache_tag or "",
        pointer_bits=struct.calcsize("P") * 8,
        torch_version=torch_version,
        torch_cuda=torch_cuda,
        native_path=str(source_runtime["native_filename"]),
        native_sha256=source_runtime["native_sha256"],
        native_bytes=source_runtime["native_bytes"],
        native_source_sha256=source_runtime["native_source_sha256"],
        compiler=source_runtime["compiler"],
        cxx_standard=source_runtime["cxx_standard"],
        backend=source_runtime["backend"],
        protocol_version=source_runtime["protocol_version"],
    )
    block = dict(source_runtime)
    block.update(
        os_name=identity.os_name,
        python_implementation=identity.python_implementation,
        python_version=identity.python_version,
        python_cache_tag=identity.python_cache_tag,
        pointer_bits=identity.pointer_bits,
        torch_version=torch_version,
        torch_cuda=torch_cuda,
        identity_sha256=identity.identity_sha256,
    )
    return block


def build_one(model_id: str, source_bundle: Path, checkpoint: Path, output_root: Path,
              *, torch_version: str, torch_cuda: str) -> dict:
    deployment = json.loads((source_bundle / "deployment.json").read_text(encoding="utf-8"))
    manifest = json.loads((source_bundle / "manifest.json").read_text(encoding="utf-8"))
    block = deployment["checkpoint"]
    if not checkpoint.is_file():
        raise SystemExit(f"CHECKPOINT_MISSING:{model_id}")
    sha, size = _sha256(checkpoint)
    if (sha, size) != (block["sha256"], block["bytes"]):
        raise SystemExit(f"CHECKPOINT_IDENTITY_MISMATCH:{model_id}")
    expectation = CheckpointExpectation(
        filename=block["filename"], bytes=block["bytes"], sha256=block["sha256"],
        schema=block["schema"], algorithm=block["algorithm"],
        architecture=block["architecture"], protocol_version=block["protocol_version"],
        iteration=block["iteration"], classification=block["classification"],
        resume_classification=block["resume_classification"],
        training_resume_allowed=block["training_resume_allowed"],
        resolved_config_hash=block["resolved_config_hash"], source_hash=block["source_hash"],
        source_parent_commit=block["source_parent_commit"],
        training_backend_identity_hash=block["training_backend_identity_hash"],
        payload_variant=block["payload_variant"], model_version=manifest["model_version"],
        training_version=manifest["training_version"])
    runtime = _runtime_block(deployment["runtime"], torch_version=torch_version,
                             torch_cuda=torch_cuda)
    descriptor = build_deployment(checkpoint=expectation,
                                  source_fingerprint=deployment["source_fingerprint"],
                                  runtime=runtime)
    target = output_root / f"{model_id}-device"
    target.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(descriptor, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
           + "\n").encode("utf-8")
    (target / "deployment.json").write_bytes(raw)
    manifest.update(
        runtime_contract=DEVICE_CONTRACT,
        artifact="deployment.json",
        artifact_format="guance_marvel_v2_deployment_v1",
        artifact_sha256=hashlib.sha256(raw).hexdigest(),
        artifact_bytes=len(raw),
        capabilities={"recommend": True, "next_token_policy": True,
                      "complete_action": True, "confirmed_decision": True},
        classification="evaluation_only",
        readiness={"experimental": True, "production_ready": False, "strength_claim": False},
    )
    manifest_raw = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
                    + "\n").encode("utf-8")
    (target / "manifest.json").write_bytes(manifest_raw)
    sums = [f"{hashlib.sha256(raw).hexdigest()}  deployment.json",
            f"{hashlib.sha256(manifest_raw).hexdigest()}  manifest.json"]
    (target / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")
    return {
        "model_id": model_id, "model_version": manifest["model_version"],
        "checkpoint_sha256": sha, "checkpoint_bytes": size,
        "checkpoint_path_kind": "external_read_only",
        "deployment_sha256": sums[0].split()[0], "deployment_bytes": len(raw),
        "manifest_sha256": sums[1].split()[0], "runtime_contract": DEVICE_CONTRACT,
        "torch_version": torch_version, "torch_cuda": torch_cuda,
        "runtime_identity_sha256": runtime["identity_sha256"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--torch-version", required=True)
    parser.add_argument("--torch-cuda", required=True)
    args = parser.parse_args()
    output_root = args.output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    records = []
    for model_id, (source_bundle, checkpoint) in DEFAULT_BUNDLES.items():
        records.append(build_one(model_id, source_bundle, checkpoint, output_root,
                                 torch_version=args.torch_version,
                                 torch_cuda=args.torch_cuda))
    (output_root / "bundles.json").write_text(
        json.dumps({"schema": "guance-gpu-first-bundles-v1", "bundles": records},
                   ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"bundles": records}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
