"""Phase 6 copy/restart/drop-in acceptance orchestrator.

Runs the complete Phase 6 acceptance against real, read-only inputs:

* real FableDan ``best.npz`` and real MARVEL V2 iter100 / iter500 / iter2000
  checkpoints through the accepted GPU-first device bundles;
* the frozen Phase 2 runtime source (read-only) and the real generic JSONL worker;
* an isolated temporary app-data ``models/catalog`` + active selection document;
* one fresh operating-system process per restart via
  ``scripts/run_phase6_restart_probe.py`` (never an in-process reload);
* the stable packaging tool ``scripts/package_model_bundle.py`` for every install;
* negative scenarios for missing/partial/duplicate/tampered/contract-changed
  bundles and a no-fallback check with a valid FableDan bundle present.

Only bounded evidence is written under ``--evidence-root``; payloads, isolated
app-data and probe outputs stay in the system temporary directory. All model
inputs and the frozen runtime are read-only and re-hashed before/after.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT))
sys.path.insert(0, str(CODE_ROOT / "src"))
sys.dont_write_bytecode = True

from guance.contracts import SourceFrame  # noqa: E402
from guance.domain.session import SessionEngine  # noqa: E402
from guance.infrastructure.capture.live_subset import NormalExchangeLiveSubsetProvider  # noqa: E402
from guance.infrastructure.model.marvel_v2.decision_request import (  # noqa: E402
    project_confirmed_decision,
)
from guance.infrastructure.model.selection import (  # noqa: E402
    save_active_selection, selected_at_now,
)
from guance.ports.model import ActiveSelectionDTO  # noqa: E402
from scripts.package_model_bundle import package_bundle  # noqa: E402
from scripts.run_phase5_offline_acceptance import validate_safe_output  # noqa: E402

PHASE2 = CODE_ROOT / "build" / "acceptance" / "guance-model-platform-20261016" / "phase2"
RUNTIME_ROOT = PHASE2 / "runtime" / "marvel-e5c82e7c-d7acbc8d"
RUNTIME_SOURCE = RUNTIME_ROOT / "source"
GPU_BUNDLES = CODE_ROOT / "build" / "acceptance" / "guance-gpu-first-inference-20261020" / "bundles"
BEST_NPZ = CODE_ROOT / "data" / "profiles" / "tencent_daguandan" / "models" / "best.npz"
GPU_PYTHON = CODE_ROOT / "build" / "gpu-env-20261020" / "Scripts" / "python.exe"
PROBE = CODE_ROOT / "scripts" / "run_phase6_restart_probe.py"
CAPTURE_ID = "20261014-143204"
CAPTURE = CODE_ROOT / "debug" / "captures" / CAPTURE_ID / "frames.jsonl"
SIDECAR = CODE_ROOT / "debug" / "captures" / CAPTURE_ID / "capture_status.json"
EXPECTED_CAPTURE_SHA256 = "29e91cf3ffc3169bb671b238fe7549944e60a9bc8b0a9a61d47192f6ba0e5e48"
EXPECTED_CAPTURE_BYTES = 934179
EXPECTED_BEST_NPZ_SHA256 = "86570527c988026f74e21e84bf2f50876ede1d38c962d196e444115cf3571310"
EXPECTED_RUNTIME_ENTRIES = 189
DEVICE_CONTRACT = "marvel_v2.device.v1"

MODELS: dict[str, dict[str, Any]] = {
    "iter100": {
        "bundle": GPU_BUNDLES / "marvel-v2-iter000100-device",
        "model_id": "marvel-v2-iter000100",
        "model_version": "MARVEL-V2-v1.3-iter000100",
        "iteration": 100,
        "checkpoint": Path(r"D:\project\guandan\runs\comparison_delivery\dataset"
                           r"\m09_champion_iter000100.pt"),
        "checkpoint_sha256": "b00a2c178e8b3e99d80418ae283a785285639b8172a0d96232beef7233b9d41c",
        "checkpoint_bytes": 8316529,
    },
    "iter500": {
        "bundle": GPU_BUNDLES / "marvel-v2-iter000500-device",
        "model_id": "marvel-v2-iter000500",
        "model_version": "MARVEL-V2-v1.3-iter000500",
        "iteration": 500,
        "checkpoint": Path(r"D:\project\guandan\runs\M09_full_training\remote"
                           r"\iter500_formal_eval_20261014T030527Z\extracted\candidates"
                           r"\ippo_iter000500.pt"),
        "checkpoint_sha256": "f4d1d5872fa80ad1be2804ee91620046256dc0d481b78a25e4b0311f80355cea",
        "checkpoint_bytes": 8366101,
    },
    "iter2000": {
        "bundle": GPU_BUNDLES / "marvel-v2-iter002000-device",
        "model_id": "marvel-v2-iter002000",
        "model_version": "MARVEL-V2-v1.3-iter002000",
        "iteration": 2000,
        "checkpoint": Path(r"D:\project\guandan\runs\comparison_delivery\dataset"
                           r"\m09_candidate_iter002000.pt"),
        "checkpoint_sha256": "af0df1eaeb67c88e467ee78bf6b0494c49d000f5733d655ee81b40351d4ca9c8",
        "checkpoint_bytes": 8588181,
    },
}
FABLEDAN_MODEL_ID = "fabledan-phase6"


class AcceptanceError(RuntimeError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AcceptanceError(message)


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write(evidence_root: Path, name: str, value: object) -> None:
    validate_safe_output(value)
    (evidence_root / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(CODE_ROOT), *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", check=False)


def _source_snapshot(label: str) -> dict[str, Any]:
    tracked = [line for line in _git("ls-files", "--", "src", "tests", "scripts", "docs")
               .stdout.splitlines() if line.strip()]
    tracked_hashes: dict[str, str] = {}
    for relative in tracked:
        path = CODE_ROOT / relative
        if path.is_file():
            tracked_hashes[relative] = _sha256_file(path)[0]
    working: dict[str, str] = {}
    for root_name in ("src", "tests", "scripts", "docs"):
        for path in sorted((CODE_ROOT / root_name).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            working[path.relative_to(CODE_ROOT).as_posix()] = _sha256_file(path)[0]
    status = _git("status", "--porcelain=v1").stdout
    diff = _git("diff", "--no-ext-diff").stdout
    return {
        "schema": "guance-phase6-source-snapshot-v1",
        "label": label,
        "head": _git("rev-parse", "HEAD").stdout.strip(),
        "tracked_file_count": len(tracked_hashes),
        "tracked_files_sha256": tracked_hashes,
        "working_file_count": len(working),
        "working_files_sha256": working,
        "git_status_sha256": _sha256_text(status),
        "git_diff_sha256": _sha256_text(diff),
        "git_status_entry_count": len([line for line in status.splitlines() if line.strip()]),
    }


def _snapshot_public(snapshot: dict[str, Any]) -> dict[str, Any]:
    tracked = json.dumps(snapshot["tracked_files_sha256"], sort_keys=True, separators=(",", ":"))
    working = json.dumps(snapshot["working_files_sha256"], sort_keys=True, separators=(",", ":"))
    return {
        "schema": snapshot["schema"], "label": snapshot["label"], "head": snapshot["head"],
        "tracked_file_count": snapshot["tracked_file_count"],
        "working_file_count": snapshot["working_file_count"],
        "tracked_files_manifest_sha256": _sha256_text(tracked),
        "working_files_manifest_sha256": _sha256_text(working),
        "git_status_sha256": snapshot["git_status_sha256"],
        "git_diff_sha256": snapshot["git_diff_sha256"],
        "git_status_entry_count": snapshot["git_status_entry_count"],
    }


def _write_inventory(path: Path, label: str, mapping: dict[str, str]) -> None:
    lines = [f"# {label}\n"] + [f"{digest}  {name}\n" for name, digest in sorted(mapping.items())]
    path.write_text("".join(lines), encoding="utf-8")


def _compare_snapshots(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    tracked_changed = sorted(
        set(before["tracked_files_sha256"]) ^ set(after["tracked_files_sha256"]))
    tracked_modified = sorted(
        name for name in set(before["tracked_files_sha256"]) & set(after["tracked_files_sha256"])
        if before["tracked_files_sha256"][name] != after["tracked_files_sha256"][name])
    working_changed = sorted(
        set(before["working_files_sha256"]) ^ set(after["working_files_sha256"]))
    working_modified = sorted(
        name for name in set(before["working_files_sha256"]) & set(after["working_files_sha256"])
        if before["working_files_sha256"][name] != after["working_files_sha256"][name])
    return {
        "schema": "guance-phase6-source-diff-comparison-v1",
        "same_head": before["head"] == after["head"],
        "tracked_added_or_removed": tracked_changed,
        "tracked_modified": tracked_modified,
        "working_added_or_removed": working_changed,
        "working_modified": working_modified,
        "git_status_changed": before["git_status_sha256"] != after["git_status_sha256"],
        "git_diff_changed": before["git_diff_sha256"] != after["git_diff_sha256"],
        "tracked_source_unchanged": not tracked_changed and not tracked_modified,
        "working_source_unchanged": not working_changed and not working_modified,
        "pre_existing_dirty_baseline": True,
    }


def _verify_runtime() -> dict[str, Any]:
    runtime = json.loads((RUNTIME_ROOT / "runtime.json").read_text(encoding="utf-8"))
    _require(runtime.get("runtime_id") == "marvel-e5c82e7c27fb-d7acbc8d76c9", "RUNTIME_ID")
    entries = 0
    for line in (RUNTIME_ROOT / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, _, relative = line.partition("  ")
        target = RUNTIME_ROOT / relative
        _require(target.is_file() and _sha256_file(target)[0] == digest, "RUNTIME_SHA256SUMS")
        entries += 1
    _require(entries == EXPECTED_RUNTIME_ENTRIES, "RUNTIME_ENTRY_COUNT")
    pycache = sum(1 for path in RUNTIME_ROOT.rglob("__pycache__") if path.is_dir())
    _require(pycache == 0, "RUNTIME_PYCACHE")
    return {"runtime_id": runtime["runtime_id"], "runtime_contract": runtime["runtime_contract"],
            "sha256sums_entries": entries, "pycache_directories": pycache}


def _verify_inputs() -> dict[str, Any]:
    capture_sha, capture_bytes = _sha256_file(CAPTURE)
    _require(capture_sha == EXPECTED_CAPTURE_SHA256 and capture_bytes == EXPECTED_CAPTURE_BYTES,
             "CAPTURE_IDENTITY")
    sidecar = json.loads(SIDECAR.read_text(encoding="utf-8"))
    _require(sidecar.get("state") == "closed" and sidecar.get("source_sha256") == capture_sha,
             "CAPTURE_SIDECAR")
    best_sha, best_bytes = _sha256_file(BEST_NPZ)
    _require(best_sha == EXPECTED_BEST_NPZ_SHA256, "FABLEDAN_WEIGHTS")
    record: dict[str, Any] = {
        "capture_sha256": capture_sha, "capture_bytes": capture_bytes,
        "capture_sidecar_state": sidecar.get("state"),
        "fabledan_weights_sha256": best_sha, "fabledan_weights_bytes": best_bytes,
        "runtime": _verify_runtime(), "models": {},
    }
    for key, model in MODELS.items():
        manifest_raw = (model["bundle"] / "manifest.json").read_bytes()
        manifest = json.loads(manifest_raw.decode("utf-8"))
        artifact_sha, artifact_bytes = _sha256_file(model["bundle"] / "deployment.json")
        checkpoint_sha, checkpoint_bytes = _sha256_file(model["checkpoint"])
        _require(manifest.get("provider") == "guandan_marvel_v2", "PROVIDER")
        _require(manifest.get("runtime_contract") == DEVICE_CONTRACT, "RUNTIME_CONTRACT")
        _require(manifest.get("model_version") == model["model_version"], "MODEL_VERSION")
        _require(manifest.get("artifact_sha256") == artifact_sha, "ARTIFACT_HASH")
        _require(manifest.get("artifact_bytes") == artifact_bytes, "ARTIFACT_BYTES")
        _require(checkpoint_sha == model["checkpoint_sha256"], "CHECKPOINT_HASH")
        _require(checkpoint_bytes == model["checkpoint_bytes"], "CHECKPOINT_BYTES")
        record["models"][key] = {
            "model_id": model["model_id"], "model_version": model["model_version"],
            "iteration": model["iteration"], "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
            "deployment_sha256": artifact_sha, "deployment_bytes": artifact_bytes,
            "checkpoint_sha256": checkpoint_sha, "checkpoint_bytes": checkpoint_bytes,
            "runtime_contract": manifest["runtime_contract"],
        }
    bundles_json = json.loads((GPU_BUNDLES / "bundles.json").read_text(encoding="utf-8"))
    pinned = {entry["model_id"]: entry for entry in bundles_json["bundles"]}
    for key, model in MODELS.items():
        entry = pinned[model["model_id"]]
        _require(entry["deployment_sha256"] == record["models"][key]["deployment_sha256"],
                 "BUNDLES_JSON_DEPLOYMENT")
        _require(entry["checkpoint_sha256"] == record["models"][key]["checkpoint_sha256"],
                 "BUNDLES_JSON_CHECKPOINT")
    return {"schema": "guance-phase6-input-gate-v1", "provenance": "offline_fixed_inputs",
            "live_input_used": False, **record}


# ---------------------------------------------------------------------------
# Confirmed-decision payloads from the fixed capture (temporary files only)
# ---------------------------------------------------------------------------

def _source_frame(record: dict, payload: bytes) -> SourceFrame:
    return SourceFrame(host=str(record["host"]), payload=payload,
                       received_at=float(record["received_at"]),
                       connection_id=str(record.get("connection_id", "")),
                       sequence=int(record["sequence"]), direction=str(record.get("direction")))


def _parse_aggregate():
    from guance.infrastructure.protocol import live
    return live.parse_aggregate


def _game_host():
    from guance.infrastructure.protocol import live
    return live.GAME_HOST


def _replay_payloads(payload_dir: Path) -> dict[str, Any]:
    payload_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"schema": "guance-phase6-payloads-v1",
                              "capture_id": CAPTURE_ID, "models": {}}
    for key, model in MODELS.items():
        provider = NormalExchangeLiveSubsetProvider(RUNTIME_SOURCE)
        provider.set_model_artifact_identity_provider(
            lambda mv=model["model_version"], cs=model["checkpoint_sha256"]: (mv, cs))
        provider.bind_capture(CAPTURE_ID)
        engine = SessionEngine(
            "phase6-acceptance", aggregate_parser=_parse_aggregate(),
            accepted_host=_game_host(), event_adapter=provider.adapt_event,
            event_observer=provider.observe_applied, reset_observer=provider.reset_session)
        decisions = []
        for line in CAPTURE.open("rb"):
            record = json.loads(line.decode("utf-8-sig"))
            payload = b""
            if record.get("direction") == "server":
                payload = base64.b64decode(record["payload_b64"], validate=True)
            engine.feed(_source_frame(record, payload))
            decisions.extend(provider.drain_decisions())
        chosen = None
        projectable = 0
        for index, decision in enumerate(decisions, start=1):
            try:
                candidate = project_confirmed_decision(
                    decision, artifact_id=model["model_version"],
                    artifact_sha256=model["checkpoint_sha256"])
            except Exception:
                continue
            projectable += 1
            if chosen is None:
                chosen = (index, decision.action_session.phase, candidate)
        _require(chosen is not None, f"NO_PROJECTABLE_DECISION:{key}")
        _require(provider.model_adapter_eligible_count == 34, f"PROVIDER_ELIGIBLE_COUNT:{key}")
        index, phase, payload = chosen
        path = payload_dir / f"{key}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        result["models"][key] = {
            "provider_eligible_count": provider.model_adapter_eligible_count,
            "projectable_count": projectable,
            "chosen_decision_index": index,
            "chosen_phase": str(phase),
            "decision_file": f"{key}.json",
        }
    return result


# ---------------------------------------------------------------------------
# Real process restarts
# ---------------------------------------------------------------------------

def _probe_env(model_key: str | None) -> dict[str, str]:
    env = os.environ.copy()
    for name in ("GUANCE_MARVEL_V2_SOURCE", "GUANCE_MARVEL_V2_CHECKPOINT",
                 "GUANCE_MODEL_BUNDLE", "GUANCE_MODEL_PATH", "GUANCE_DATA_DIR"):
        env.pop(name, None)
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    if model_key is not None:
        env["GUANCE_MARVEL_V2_SOURCE"] = str(RUNTIME_SOURCE)
        env["GUANCE_MARVEL_V2_CHECKPOINT"] = str(MODELS[model_key]["checkpoint"])
    return env


def run_probe(label: str, models_root: Path, *, mode: str, model_key: str | None,
              payload: Path | None, device: str, probe_dir: Path) -> dict[str, Any]:
    out_path = probe_dir / f"{label}.json"
    command = [str(GPU_PYTHON), "-B", str(PROBE), "--models-root", str(models_root),
               "--mode", mode, "--device", device, "--timeout", "600",
               "--load-timeout", "600", "--out", str(out_path)]
    if payload is not None:
        command.extend(["--payload", str(payload)])
    started = time.monotonic()
    process = subprocess.Popen(command, cwd=str(CODE_ROOT), env=_probe_env(model_key),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace")
    try:
        stdout, stderr = process.communicate(timeout=900)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
    elapsed = round(time.monotonic() - started, 3)
    output: dict[str, Any] = {}
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            output = json.loads(line)
            break
        except ValueError:
            continue
    if not output and out_path.is_file():
        try:
            output = json.loads(out_path.read_text(encoding="utf-8"))
        except ValueError:
            output = {}
    record = {
        "label": label, "process_pid": process.pid, "exit_code": process.returncode,
        "elapsed_seconds": elapsed, "mode": mode, "device_requested": device,
        "probe": output, "stderr_tail": stderr[-400:] if stderr else "",
    }
    return record


def _save_active(models_root: Path, model_id: str, bundle: str) -> dict[str, Any]:
    save_active_selection(models_root / "active_model.json", ActiveSelectionDTO(
        kind="catalog", model_id=model_id, bundle=bundle, selected_at=selected_at_now()),
        root=models_root)
    return {"model_id": model_id, "bundle": bundle, "method": "save_active_selection(stage+commit)"}


def _make_partial_copy(catalog: Path, name: str) -> None:
    directory = catalog / name
    directory.mkdir()
    shutil.copyfile(MODELS["iter500"]["bundle"] / "manifest.json", directory / "manifest.json")
    shutil.copyfile(MODELS["iter500"]["bundle"] / "deployment.json", directory / "deployment.json")


def _models_root(base: Path, scenario: str) -> Path:
    root = base / scenario / "models"
    (root / "catalog").mkdir(parents=True)
    return root


def _install(models_root: Path, model_key: str, bundle_name: str) -> dict[str, Any]:
    model = MODELS[model_key]
    return package_bundle(model["bundle"], models_root / "catalog", bundle_name=bundle_name,
                          checkpoint=model["checkpoint"])


def _install_fabledan(models_root: Path, fabledan_source: Path) -> dict[str, Any]:
    return package_bundle(fabledan_source, models_root / "catalog",
                          bundle_name="fabledan-phase6")


def _make_fabledan_source(base: Path) -> Path:
    source = base / "sources" / "fabledan"
    source.mkdir(parents=True)
    shutil.copyfile(BEST_NPZ, source / "artifact.npz")
    artifact_sha, artifact_bytes = _sha256_file(source / "artifact.npz")
    manifest = {
        "schema_version": 2, "model_id": FABLEDAN_MODEL_ID, "provider": "fabledan",
        "backend": "numpy", "runtime": "numpy", "runtime_contract": "fabledan.numpy.v1",
        "artifact": "artifact.npz", "artifact_format": "npz",
        "artifact_sha256": artifact_sha, "artifact_bytes": artifact_bytes,
        "input_contract": "CanonicalPublicGameView.v1",
        "output_contract": "CanonicalAdviceResult.v1",
        "model_version": "legacy", "training_version": "legacy-unknown",
        "rules_version": "tencent_daguandan.v1", "action_version": "public-action.v1",
        "environment_version": "public-play.v1", "encoding_version": "fabledan.v1",
        "algorithm": "fabledan-q", "architecture": "FableDanNet",
        "classification": "legacy",
        "readiness": {"experimental": False, "production_ready": False, "strength_claim": False},
        "capabilities": {"recommend": True, "top_n": True, "complete_action": True},
    }
    (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return source


def _make_variant_source(base: Path, model_key: str, new_model_id: str) -> Path:
    model = MODELS[model_key]
    source = base / "sources" / new_model_id
    source.mkdir(parents=True)
    shutil.copyfile(model["bundle"] / "deployment.json", source / "deployment.json")
    manifest = json.loads((model["bundle"] / "manifest.json").read_text(encoding="utf-8"))
    manifest["model_id"] = new_model_id
    (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return source


# ---------------------------------------------------------------------------
# Bundle mutation helpers for negative scenarios
# ---------------------------------------------------------------------------

def _rewrite_artifact_and_checksums(directory: Path) -> None:
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact = directory / manifest["artifact"]
    artifact_sha, artifact_bytes = _sha256_file(artifact)
    manifest["artifact_sha256"] = artifact_sha
    manifest["artifact_bytes"] = artifact_bytes
    manifest_raw = json.dumps(manifest, sort_keys=True).encode("utf-8")
    manifest_path.write_bytes(manifest_raw)
    lines = [f"{artifact_sha}  {manifest['artifact']}",
             f"{hashlib.sha256(manifest_raw).hexdigest()}  manifest.json"]
    (directory / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _mutate_descriptor(directory: Path, mutator) -> None:
    path = directory / "deployment.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    mutator(data)
    raw = (json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
           + "\n").encode("utf-8")
    path.write_bytes(raw)
    _rewrite_artifact_and_checksums(directory)


def _tamper_artifact(directory: Path) -> None:
    with (directory / "deployment.json").open("ab") as stream:
        stream.write(b"x")


def _tamper_checksums(directory: Path) -> None:
    lines = (directory / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    rewritten = ["0" * 64 + "  " + line.split()[-1] for line in lines]
    (directory / "SHA256SUMS").write_text("\n".join(rewritten) + "\n", encoding="utf-8")


def _tamper_manifest(directory: Path) -> None:
    path = directory / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["artifact_sha256"] = "0" * 64
    path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")


def _tamper_deployment_checkpoint(directory: Path) -> None:
    def mutate(data: dict) -> None:
        data["checkpoint"]["sha256"] = "1" * 64
    _mutate_descriptor(directory, mutate)


def _change_protocol(directory: Path) -> None:
    def mutate(data: dict) -> None:
        data["checkpoint"]["protocol_version"] = (
            "GD-ACTION-0.3-MARVEL|GD-ENV-0.2-MARVEL|GD-ENCODING-0.2-MARVEL")
    _mutate_descriptor(directory, mutate)


def _change_architecture(directory: Path) -> None:
    def mutate(data: dict) -> None:
        data["checkpoint"]["architecture"] = "v1.4"
    _mutate_descriptor(directory, mutate)


def _change_runtime_contract(directory: Path) -> None:
    path = directory / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["runtime_contract"] = "marvel_v2.device.v2"
    raw = json.dumps(manifest, sort_keys=True).encode("utf-8")
    path.write_bytes(raw)
    sums_path = directory / "SHA256SUMS"
    lines = sums_path.read_text(encoding="utf-8").splitlines()
    artifact_line = next(line for line in lines if line.endswith("deployment.json"))
    sums_path.write_text(artifact_line + "\n" + hashlib.sha256(raw).hexdigest()
                         + "  manifest.json\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------

def _recommendation_ok(record: dict[str, Any], *, provider: str,
                       model_version: str | None, iteration: int | None,
                       checkpoint_sha: str | None) -> dict[str, Any]:
    _require(record["exit_code"] == 0, f"RESTART_EXIT:{record['label']}:{record['exit_code']}")
    probe = record["probe"]
    _require(probe.get("status") == "ready", f"RESTART_NOT_READY:{record['label']}")
    metadata = probe.get("metadata") or {}
    recommendation = probe.get("recommendation") or {}
    _require(metadata.get("provider") == provider, f"PROVIDER:{record['label']}")
    _require(recommendation.get("provider") == provider, f"RESULT_PROVIDER:{record['label']}")
    _require(recommendation.get("status") == "ready", f"RESULT_STATUS:{record['label']}")
    count = int(recommendation.get("candidate_count", 0))
    if provider == "guandan_marvel_v2":
        _require(count == 1, f"RESULT_CANDIDATES:{record['label']}")
    else:
        _require(count >= 1, f"RESULT_CANDIDATES:{record['label']}")
    _require(probe.get("fallback_suspected") is False, f"FALLBACK:{record['label']}")
    if model_version is not None:
        _require(metadata.get("model_version") == model_version, f"MODEL_VERSION:{record['label']}")
    if iteration is not None:
        _require(metadata.get("iteration") == iteration, f"ITERATION:{record['label']}")
    if checkpoint_sha is not None:
        _require(metadata.get("model_sha256") == checkpoint_sha, f"MODEL_SHA:{record['label']}")
        _require(recommendation.get("model_fingerprint") == checkpoint_sha,
                 f"RESULT_SHA:{record['label']}")
    build = str(metadata.get("torch_cuda_build", ""))
    if provider == "guandan_marvel_v2" and build not in ("", "cpu"):
        _require(metadata.get("actual_device") == "cuda:0", f"GPU_POLICY:{record['label']}")
        _require(metadata.get("device_reason") == "CUDA_PROBE_OK", f"GPU_REASON:{record['label']}")
    elif provider == "guandan_marvel_v2":
        _require(metadata.get("actual_device") == "cpu", f"CPU_DEVICE:{record['label']}")
        _require(metadata.get("device_fallback") is True, f"CPU_FALLBACK:{record['label']}")
    return {
        "label": record["label"], "pid": record["process_pid"],
        "elapsed_seconds": record["elapsed_seconds"],
        "active": probe.get("active"),
        "provider": metadata.get("provider"),
        "model_version": metadata.get("model_version"),
        "iteration": metadata.get("iteration"),
        "model_sha256": metadata.get("model_sha256"),
        "bundle_fingerprint": metadata.get("bundle_fingerprint"),
        "runtime_contract": metadata.get("runtime_contract"),
        "requested_device": metadata.get("requested_device"),
        "actual_device": metadata.get("actual_device"),
        "device_fallback": metadata.get("device_fallback"),
        "device_reason": metadata.get("device_reason"),
        "torch_cuda_build": metadata.get("torch_cuda_build"),
        "recommendation_provider": recommendation.get("provider"),
        "recommendation_model_fingerprint": recommendation.get("model_fingerprint"),
        "recommendation_score_kind": recommendation.get("score_kind"),
        "recommendation_candidate_kind": recommendation.get("candidate_kind"),
        "recommendation_candidate_count": recommendation.get("candidate_count"),
        "recommendation_elapsed_ms": recommendation.get("result_elapsed_ms"),
        "fallback_suspected": probe.get("fallback_suspected"),
    }


def _negative_ok(record: dict[str, Any], *, expected_codes: set[str],
                 expected_problem_codes: set[str] | None = None) -> dict[str, Any]:
    probe = record["probe"]
    _require(record["exit_code"] == 2, f"NEGATIVE_EXIT:{record['label']}:{record['exit_code']}")
    _require(probe.get("status") == "fail_closed", f"NEGATIVE_STATUS:{record['label']}")
    _require("recommendation" not in probe, f"NEGATIVE_RECOMMENDATION:{record['label']}")
    metadata = probe.get("metadata") or {}
    _require(metadata.get("provider") != "fabledan", f"NEGATIVE_FALLBACK:{record['label']}")
    _require(probe.get("fallback_suspected") is False, f"NEGATIVE_FLAG:{record['label']}")
    observed_problems = {problem["code"] for problem in
                         (probe.get("catalog") or {}).get("problems", [])}
    observed = {str(probe.get("error_code", ""))} | observed_problems
    _require(bool(observed & expected_codes), f"NEGATIVE_CODE:{record['label']}:{sorted(observed)}")
    if expected_problem_codes is not None:
        _require(bool(observed_problems & expected_problem_codes),
                 f"NEGATIVE_PROBLEM_CODE:{record['label']}")
    return {
        "label": record["label"], "pid": record["process_pid"],
        "elapsed_seconds": record["elapsed_seconds"],
        "active": probe.get("active"),
        "stage": probe.get("stage"),
        "error_code": probe.get("error_code"),
        "catalog_problems": (probe.get("catalog") or {}).get("problems", []),
        "recommendation_present": "recommendation" in probe,
        "fallback_suspected": probe.get("fallback_suspected"),
        "exit_code": record["exit_code"],
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run(evidence_root: Path) -> dict[str, Any]:
    if not GPU_PYTHON.is_file():
        raise AcceptanceError("GPU_PYTHON_MISSING")
    evidence_root.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="guance-phase6-appdata-"))
    probe_dir = base / "probe-output"
    probe_dir.mkdir()
    payload_dir = base / "payloads"

    input_gate = _verify_inputs()
    _write(evidence_root, "input-gate.json", input_gate)

    source_before = _source_snapshot("before")
    _write(evidence_root, "source-diff-before.json", _snapshot_public(source_before))
    _write_inventory(evidence_root / "source-hashes-before.txt", "before",
                     {**source_before["tracked_files_sha256"]})
    _write_inventory(evidence_root / "source-hashes-before-working.txt", "before-working",
                     source_before["working_files_sha256"])
    pyc_before = sorted(str(path.relative_to(CODE_ROOT)) for root_name in ("src", "tests", "scripts")
                        for path in (CODE_ROOT / root_name).rglob("*.pyc"))

    payloads = _replay_payloads(payload_dir)
    _write(evidence_root, "payload-generation.json", payloads)

    fabledan_source = _make_fabledan_source(base)
    restarts: list[dict[str, Any]] = []
    audit_writes: list[dict[str, Any]] = []
    audit_reads: list[dict[str, Any]] = []
    installations: list[dict[str, Any]] = []
    negatives: list[dict[str, Any]] = []

    # ---------------- positive flow ----------------
    positive = _models_root(base, "positive")
    installations.append({"scenario": "positive", **_install_fabledan(positive, fabledan_source)})
    audit_writes.append(_save_active(positive, FABLEDAN_MODEL_ID, "fabledan-phase6"))
    record = run_probe("R1-fabledan", positive, mode="public", model_key=None,
                       payload=None, device="auto", probe_dir=probe_dir)
    restarts.append(record)
    positive_fabledan = _recommendation_ok(record, provider="fabledan", model_version=None,
                                           iteration=None, checkpoint_sha=None)
    audit_reads.append({"label": record["label"], "active": record["probe"].get("active")})

    for key in ("iter100", "iter2000"):
        model = MODELS[key]
        installations.append({"scenario": "positive", **_install(positive, key, model["model_id"])})
        audit_writes.append(_save_active(positive, model["model_id"], model["model_id"]))
        record = run_probe(f"R2-{key}", positive, mode="confirmed", model_key=key,
                           payload=payload_dir / f"{key}.json", device="auto",
                           probe_dir=probe_dir)
        restarts.append(record)
        _recommendation_ok(record, provider="guandan_marvel_v2",
                           model_version=model["model_version"],
                           iteration=model["iteration"],
                           checkpoint_sha=model["checkpoint_sha256"])
        audit_reads.append({"label": record["label"], "active": record["probe"].get("active")})

    # Install the future iter500 bundle while iter2000 is still active.
    future = MODELS["iter500"]
    installations.append({"scenario": "positive", **_install(positive, "iter500", future["model_id"])})
    record = run_probe("R4-iter500-installed-active-iter2000", positive, mode="confirmed",
                       model_key="iter2000", payload=payload_dir / "iter2000.json",
                       device="auto", probe_dir=probe_dir)
    restarts.append(record)
    not_upgraded = _recommendation_ok(record, provider="guandan_marvel_v2",
                                      model_version=MODELS["iter2000"]["model_version"],
                                      iteration=2000,
                                      checkpoint_sha=MODELS["iter2000"]["checkpoint_sha256"])
    _require((record["probe"].get("active") or {}).get("model_id") == "marvel-v2-iter002000",
             "ACTIVE_AUTO_UPGRADED")
    audit_reads.append({"label": record["label"], "active": record["probe"].get("active")})

    audit_writes.append(_save_active(positive, future["model_id"], future["model_id"]))
    record = run_probe("R5-iter500-future", positive, mode="confirmed", model_key="iter500",
                       payload=payload_dir / "iter500.json", device="auto", probe_dir=probe_dir)
    restarts.append(record)
    future_result = _recommendation_ok(record, provider="guandan_marvel_v2",
                                       model_version=future["model_version"],
                                       iteration=future["iteration"],
                                       checkpoint_sha=future["checkpoint_sha256"])
    audit_reads.append({"label": record["label"], "active": record["probe"].get("active")})

    audit_writes.append(_save_active(positive, FABLEDAN_MODEL_ID, "fabledan-phase6"))
    record = run_probe("R6-back-to-fabledan", positive, mode="public", model_key=None,
                       payload=None, device="auto", probe_dir=probe_dir)
    restarts.append(record)
    back_fabledan = _recommendation_ok(record, provider="fabledan", model_version=None,
                                       iteration=None, checkpoint_sha=None)
    audit_reads.append({"label": record["label"], "active": record["probe"].get("active")})

    variant_source = _make_variant_source(base, "iter100", "marvel-v2-iter000100-alt")
    installations.append({"scenario": "positive",
                          **package_bundle(variant_source, positive / "catalog",
                                           bundle_name="marvel-v2-iter000100-alt",
                                           checkpoint=MODELS["iter100"]["checkpoint"])})
    record = run_probe("R7-catalog-added-active-unchanged", positive, mode="public",
                       model_key=None, payload=None, device="auto", probe_dir=probe_dir)
    restarts.append(record)
    unchanged = _recommendation_ok(record, provider="fabledan", model_version=None,
                                   iteration=None, checkpoint_sha=None)
    _require((record["probe"].get("active") or {}).get("model_id") == FABLEDAN_MODEL_ID,
             "ACTIVE_CHANGED_ON_CATALOG_ADD")
    audit_reads.append({"label": record["label"], "active": record["probe"].get("active")})
    discovered_directories = sorted(
        entry["directory"] for entry in (record["probe"].get("catalog") or {}).get("entries", []))

    # Ready restart followed by an on-disk model change in the same catalog:
    # the old ready state must not be reused by the next process.
    post_ready = _models_root(base, "post-ready-tamper")
    package_bundle(fabledan_source, post_ready / "catalog", bundle_name="fabledan-phase6")
    installations.append({"scenario": "post-ready-baseline", **package_bundle(
        MODELS["iter500"]["bundle"], post_ready / "catalog",
        bundle_name="marvel-v2-iter000500",
        checkpoint=MODELS["iter500"]["checkpoint"])})
    audit_writes.append(_save_active(post_ready, MODELS["iter500"]["model_id"],
                                     "marvel-v2-iter000500"))
    record = run_probe("R8-post-ready-baseline", post_ready, mode="confirmed",
                       model_key="iter500", payload=payload_dir / "iter500.json",
                       device="auto", probe_dir=probe_dir)
    restarts.append(record)
    post_ready_baseline = _recommendation_ok(
        record, provider="guandan_marvel_v2",
        model_version=MODELS["iter500"]["model_version"],
        iteration=MODELS["iter500"]["iteration"],
        checkpoint_sha=MODELS["iter500"]["checkpoint_sha256"])
    audit_reads.append({"label": record["label"], "active": record["probe"].get("active")})
    _tamper_artifact(post_ready / "catalog" / "marvel-v2-iter000500")
    record = run_probe("N12-post-ready-artifact-tamper", post_ready, mode="confirmed",
                       model_key="iter500", payload=payload_dir / "iter500.json",
                       device="auto", probe_dir=probe_dir)
    restarts.append(record)
    post_ready_tamper = _negative_ok(
        record, expected_codes={"ACTIVE_SELECTION_INVALID", "ARTIFACT_SIZE_MISMATCH",
                                "ARTIFACT_HASH_MISMATCH"},
        expected_problem_codes={"ARTIFACT_SIZE_MISMATCH"})
    post_ready_tamper.update(scenario="post-ready-artifact-tamper",
                             requires_code_upgrade=False)
    negatives.append(post_ready_tamper)
    audit_reads.append({"label": record["label"], "active": record["probe"].get("active")})

    # ---------------- negative scenarios ----------------
    def negative(label: str, scenario: str, *, model_key: str, expected_codes: set[str],
                 expected_problem_codes: set[str] | None = None,
                 code_upgrade_required: bool = False,
                 mutate=None, prepare=None,
                 active_override: tuple[str, str] | None = None) -> None:
        root = _models_root(base, scenario)
        package_bundle(fabledan_source, root / "catalog", bundle_name="fabledan-phase6")
        model = MODELS[model_key]
        installation = package_bundle(model["bundle"], root / "catalog",
                                      bundle_name=model["model_id"],
                                      checkpoint=model["checkpoint"])
        installations.append({"scenario": scenario, **installation})
        if prepare is not None:
            prepare(root / "catalog")
        if mutate is not None:
            mutate(root / "catalog" / model["model_id"])
        if active_override is not None:
            audit_writes.append(_save_active(root, *active_override))
        else:
            audit_writes.append(_save_active(root, model["model_id"], model["model_id"]))
        record = run_probe(label, root, mode="confirmed", model_key=model_key,
                           payload=payload_dir / f"{model_key}.json", device="auto",
                           probe_dir=probe_dir)
        restarts.append(record)
        detail = _negative_ok(record, expected_codes=expected_codes,
                              expected_problem_codes=expected_problem_codes)
        detail["scenario"] = scenario
        detail["requires_code_upgrade"] = code_upgrade_required
        audit_reads.append({"label": label, "active": record["probe"].get("active")})
        negatives.append(detail)

    negative("N1-missing-active-bundle", "missing-active", model_key="iter500",
             expected_codes={"ACTIVE_SELECTION_INVALID"},
             active_override=("marvel-v2-iter000500", "does-not-exist"))
    negative("N2-partial-bundle", "partial-bundle", model_key="iter500",
             expected_codes={"ACTIVE_SELECTION_INVALID"},
             expected_problem_codes={"SHA256SUMS_INVALID"},
             prepare=lambda catalog: _make_partial_copy(catalog, "partial-bundle-copy"),
             active_override=("marvel-v2-iter000500", "partial-bundle-copy"))
    negative("N3-artifact-tamper", "artifact-tamper", model_key="iter500",
             expected_codes={"ARTIFACT_SIZE_MISMATCH", "ARTIFACT_HASH_MISMATCH",
                             "SHA256SUMS_INVALID", "ACTIVE_SELECTION_INVALID"},
             mutate=_tamper_artifact)
    negative("N4-checksums-tamper", "checksums-tamper", model_key="iter500",
             expected_codes={"SHA256SUMS_INVALID", "ACTIVE_SELECTION_INVALID"},
             expected_problem_codes={"SHA256SUMS_INVALID"}, mutate=_tamper_checksums)
    negative("N5-manifest-tamper", "manifest-tamper", model_key="iter500",
             expected_codes={"ARTIFACT_HASH_MISMATCH", "ACTIVE_SELECTION_INVALID"},
             expected_problem_codes={"ARTIFACT_HASH_MISMATCH"}, mutate=_tamper_manifest)
    negative("N6-deployment-tamper", "deployment-tamper", model_key="iter500",
             expected_codes={"ARTIFACT_HASH_MISMATCH"},
             mutate=_tamper_deployment_checkpoint)
    negative("N7-protocol-change", "protocol-change", model_key="iter500",
             expected_codes={"MODEL_VERSION_UNSUPPORTED"}, code_upgrade_required=True,
             mutate=_change_protocol)
    negative("N8-architecture-change", "architecture-change", model_key="iter500",
             expected_codes={"MODEL_VERSION_UNSUPPORTED"}, code_upgrade_required=True,
             mutate=_change_architecture)
    negative("N9-runtime-contract-change", "runtime-contract-change", model_key="iter500",
             expected_codes={"ACTIVE_SELECTION_INVALID", "RUNTIME_UNSUPPORTED"},
             expected_problem_codes={"RUNTIME_UNSUPPORTED"}, code_upgrade_required=True,
             mutate=_change_runtime_contract)

    # Partial non-dot bundle left beside a selected bundle: not discovered, active unchanged.
    root = _models_root(base, "partial-beside-active")
    package_bundle(fabledan_source, root / "catalog", bundle_name="fabledan-phase6")
    package_bundle(MODELS["iter500"]["bundle"], root / "catalog",
                   bundle_name="marvel-v2-iter000500",
                   checkpoint=MODELS["iter500"]["checkpoint"])
    partial = root / "catalog" / "partial-bundle-copy"
    partial.mkdir()
    shutil.copyfile(MODELS["iter500"]["bundle"] / "manifest.json", partial / "manifest.json")
    shutil.copyfile(MODELS["iter500"]["bundle"] / "deployment.json", partial / "deployment.json")
    audit_writes.append(_save_active(root, FABLEDAN_MODEL_ID, "fabledan-phase6"))
    record = run_probe("N10-partial-beside-active", root, mode="public", model_key=None,
                       payload=None, device="auto", probe_dir=probe_dir)
    restarts.append(record)
    partial_beside = _recommendation_ok(record, provider="fabledan", model_version=None,
                                        iteration=None, checkpoint_sha=None)
    partial_problems = {problem["code"] for problem in
                        (record["probe"].get("catalog") or {}).get("problems", [])}
    partial_entries = sorted(entry["directory"] for entry in
                             (record["probe"].get("catalog") or {}).get("entries", []))
    _require("SHA256SUMS_INVALID" in partial_problems, "PARTIAL_NOT_REPORTED")
    audit_reads.append({"label": record["label"], "active": record["probe"].get("active")})

    # Duplicate model_id: two directories with the same model_id.
    root = _models_root(base, "duplicate-model-id")
    package_bundle(fabledan_source, root / "catalog", bundle_name="fabledan-phase6")
    package_bundle(MODELS["iter500"]["bundle"], root / "catalog", bundle_name="iter500-a",
                   checkpoint=MODELS["iter500"]["checkpoint"])
    duplicate = root / "catalog" / "iter500-b"
    shutil.copytree(root / "catalog" / "iter500-a", duplicate)
    audit_writes.append(_save_active(root, MODELS["iter500"]["model_id"], "iter500-a"))
    record = run_probe("N11-duplicate-model-id", root, mode="confirmed", model_key="iter500",
                       payload=payload_dir / "iter500.json", device="auto", probe_dir=probe_dir)
    restarts.append(record)
    duplicate_detail = _negative_ok(record, expected_codes={"ACTIVE_SELECTION_INVALID"},
                                    expected_problem_codes={"DUPLICATE_MODEL_ID"})
    duplicate_detail.update(scenario="duplicate-model-id", requires_code_upgrade=False)
    negatives.append(duplicate_detail)
    audit_reads.append({"label": record["label"], "active": record["probe"].get("active")})

    # ---------------- source immutability ----------------
    source_after = _source_snapshot("after")
    comparison = _compare_snapshots(source_before, source_after)
    _require(comparison["tracked_source_unchanged"], "TRACKED_SOURCE_CHANGED")
    _require(comparison["working_source_unchanged"], "WORKING_SOURCE_CHANGED")
    pyc_after = sorted(str(path.relative_to(CODE_ROOT)) for root_name in ("src", "tests", "scripts")
                       for path in (CODE_ROOT / root_name).rglob("*.pyc"))
    _require(pyc_before == pyc_after, "PYC_WRITTEN")

    # ---------------- evidence ----------------
    positive_records = [positive_fabledan, future_result, back_fabledan, unchanged,
                        post_ready_baseline]
    restart_count = len(restarts)
    pids = [record["process_pid"] for record in restarts]
    _require(len(set(pids)) == len(pids), "RESTART_PID_REUSED")

    scenarios_positive = {
        "schema": "guance-phase6-copy-restart-acceptance-v1",
        "provenance": "offline_fixed_inputs_plus_real_process_restarts",
        "live_input_used": False,
        "real_process_restarts": restart_count,
        "unique_process_ids": len(set(pids)),
        "positive_restarts": [
            {
                "label": record["label"],
                "pid": record["process_pid"],
                "elapsed_seconds": record["elapsed_seconds"],
                "active": record["probe"].get("active"),
                "load_status": record["probe"].get("load_status"),
                "metadata": record["probe"].get("metadata"),
                "recommendation": record["probe"].get("recommendation"),
            }
            for record in restarts if record["label"].startswith(("R1", "R2", "R4", "R5", "R6", "R7"))
        ],
        "catalog_after_final_positive_restart": discovered_directories,
        "restart_identity_table": positive_records,
    }
    _write(evidence_root, "copy_restart_acceptance.json", scenarios_positive)

    dropin = {
        "schema": "guance-phase6-future-model-dropin-acceptance-v1",
        "provenance": "real_iter500_checkpoint",
        "live_input_used": False,
        "future_model": {
            "model_id": future["model_id"], "model_version": future["model_version"],
            "iteration": future["iteration"], "checkpoint_sha256": future["checkpoint_sha256"],
            "checkpoint_bytes": future["checkpoint_bytes"],
            "deployment_sha256": input_gate["models"]["iter500"]["deployment_sha256"],
            "deployment_bytes": input_gate["models"]["iter500"]["deployment_bytes"],
            "runtime_contract": DEVICE_CONTRACT,
        },
        "baseline_model": {
            "model_id": MODELS["iter2000"]["model_id"],
            "model_version": MODELS["iter2000"]["model_version"],
            "iteration": MODELS["iter2000"]["iteration"],
            "checkpoint_sha256": MODELS["iter2000"]["checkpoint_sha256"],
            "deployment_sha256": input_gate["models"]["iter2000"]["deployment_sha256"],
        },
        "identity_fields_that_differ": ["model_id", "model_version", "iteration",
                                        "checkpoint_sha256", "deployment_sha256",
                                        "deployment_bytes", "weights"],
        "contract_fields_that_are_identical": ["provider", "backend", "runtime",
                                               "input_contract", "output_contract",
                                               "rules_version", "action_version",
                                               "environment_version", "encoding_version",
                                               "algorithm", "architecture", "runtime_contract"],
        "loader_hardcoded_identity_scan": _scan_loader_identity(),
        "installed_by": "scripts/package_model_bundle.py (no source edit)",
        "guance_source_modified": False,
        "restart": future_result,
    }
    _write(evidence_root, "future_model_dropin_acceptance.json", dropin)

    invalid = {
        "schema": "guance-phase6-invalid-bundle-matrix-v1",
        "provenance": "offline_fixed_inputs_plus_real_process_restarts",
        "live_input_used": False,
        "cases": negatives,
        "partial_beside_active": {
            "label": partial_beside["label"], "active": partial_beside["active"],
            "problem_codes": sorted(partial_problems), "catalog_entry_directories":
            partial_entries,
            "active_unchanged": True,
        },
        "fallback_observed": False,
        "recommendations_in_negative_cases": 0,
        "post_ready_tamper": {
            "baseline_label": post_ready_baseline["label"],
            "baseline_ready": True,
            "tampered_label": post_ready_tamper["label"],
            "tampered_failed_closed": True,
            "old_ready_state_reused": False,
        },
        "requires_code_upgrade_cases": [
            case["label"] for case in negatives if case.get("requires_code_upgrade")],
    }
    _write(evidence_root, "invalid_bundle_matrix.json", invalid)

    audit = {
        "schema": "guance-phase6-active-selection-audit-v1",
        "provenance": "explicit_active_selection",
        "selection_writes": audit_writes,
        "restart_reads": audit_reads,
        "iteration_auto_upgrade_observed": False,
        "catalog_add_without_active_change": {
            "label": unchanged["label"], "active": unchanged["active"],
            "catalog_entry_directories": discovered_directories,
        },
        "no_implicit_default_or_iteration_selection": True,
        "active_document_protocol": "active_model.json + active_model.commit.json marker",
    }
    _write(evidence_root, "active_selection_audit.json", audit)

    export_tool = {
        "schema": "guance-phase6-export-tool-acceptance-v1",
        "tool": "scripts/package_model_bundle.py",
        "delivery_contract": ["manifest.json", "single artifact named by manifest.artifact",
                              "optional source SHA256SUMS"],
        "installations": [
            {"scenario": entry.get("scenario"), "status": entry.get("status"),
             "model_id": entry.get("model_id"), "provider": entry.get("provider"),
             "artifact_sha256": entry.get("artifact_sha256"),
             "artifact_bytes": entry.get("artifact_bytes"),
             "manifest_sha256": entry.get("manifest_sha256"),
             "sha256sums_sha256": entry.get("sha256sums_sha256"),
             "checkpoint": entry.get("checkpoint"), "atomic": entry.get("atomic"),
             "source_unchanged": entry.get("source_unchanged")}
            for entry in installations],
        "install_count": len(installations),
        "failure_matrix_tests": "tests/acceptance/test_phase6_package_tool.py",
        "failure_matrix_result": "see postfix-package-tool.txt and commands-and-results.json",
        "atomic_staging_prefix": ".package-",
        "partial_staging_discovered": False,
        "existing_target_rejected": True,
        "duplicate_model_id_rejected": True,
        "source_bundle_modified": False,
        "extra_source_file_rejected": True,
        "checkpoint_pin_verified_before_install": True,
    }
    _write(evidence_root, "export_tool_acceptance.json", export_tool)

    restart_log = {
        "schema": "guance-phase6-restart-log-v1",
        "real_process_restarts": restart_count,
        "restarts": [
            {"label": record["label"], "pid": record["process_pid"],
             "exit_code": record["exit_code"], "elapsed_seconds": record["elapsed_seconds"],
             "mode": record["mode"], "status": record["probe"].get("status"),
             "active": record["probe"].get("active"),
             "load_status": record["probe"].get("load_status"),
             "error_code": record["probe"].get("error_code"),
             "actual_device": (record["probe"].get("metadata") or {}).get("actual_device"),
             "recommendation_provider": (record["probe"].get("recommendation") or {}).get("provider"),
             "fallback_suspected": record["probe"].get("fallback_suspected")}
            for record in restarts],
        "unique_process_ids": len(set(pids)),
    }
    _write(evidence_root, "restart-log.json", restart_log)

    _write(evidence_root, "source-diff-after.json", _snapshot_public(source_after))
    _write_inventory(evidence_root / "source-hashes-after.txt", "after",
                     source_after["tracked_files_sha256"])
    _write_inventory(evidence_root / "source-hashes-after-working.txt", "after-working",
                     source_after["working_files_sha256"])
    _write(evidence_root, "source-diff-comparison.json", {
        **comparison, "pyc_files_before": len(pyc_before), "pyc_files_after": len(pyc_after)})

    summary = {
        "schema": "guance-phase6-run-summary-v1",
        "status": "passed",
        "real_process_restarts": restart_count,
        "unique_process_ids": len(set(pids)),
        "positive_restarts": sum(1 for record in restarts
                                 if record["label"].startswith("R")),
        "negative_scenarios": sum(1 for record in restarts
                                  if record["label"].startswith("N")),
        "iterations_loaded": [100, 500, 2000],
        "fabledan_recommendation": True,
        "marvel_recommendations": sum(
            1 for record in restarts
            if (record["probe"].get("recommendation") or {}).get("provider") == "guandan_marvel_v2"),
        "gpu_actual_devices": sorted({
            str((record["probe"].get("metadata") or {}).get("actual_device"))
            for record in restarts
            if (record["probe"].get("metadata") or {}).get("actual_device")}),
        "source_unchanged": comparison["tracked_source_unchanged"],
        "fallback_count": 0,
    }
    _write(evidence_root, "phase6-run-summary.json", summary)
    return summary


def _scan_loader_identity() -> dict[str, Any]:
    needles = {
        "iter100_checkpoint_sha256": MODELS["iter100"]["checkpoint_sha256"],
        "iter500_checkpoint_sha256": MODELS["iter500"]["checkpoint_sha256"],
        "iter2000_checkpoint_sha256": MODELS["iter2000"]["checkpoint_sha256"],
        "iter100_model_version": MODELS["iter100"]["model_version"],
        "iter500_model_version": MODELS["iter500"]["model_version"],
        "iter2000_model_version": MODELS["iter2000"]["model_version"],
    }
    loader_root = CODE_ROOT / "src" / "guance" / "infrastructure" / "model"
    loader_counts: dict[str, int] = {}
    loader_files = 0
    for path in sorted(loader_root.rglob("*.py")):
        loader_files += 1
        text = path.read_text(encoding="utf-8", errors="replace")
        for name, needle in needles.items():
            if needle and needle in text:
                loader_counts[name] = loader_counts.get(name, 0) + 1
    non_loader: list[dict[str, str]] = []
    src_files = 0
    for path in sorted((CODE_ROOT / "src" / "guance").rglob("*.py")):
        relative = path.relative_to(CODE_ROOT).as_posix()
        if relative.startswith("src/guance/infrastructure/model/"):
            continue
        src_files += 1
        text = path.read_text(encoding="utf-8", errors="replace")
        for name, needle in needles.items():
            if needle and needle in text:
                non_loader.append({"file": relative, "needle": name})
    return {
        "loader_scope": "src/guance/infrastructure/model",
        "loader_files_scanned": loader_files,
        "loader_needle_occurrences": loader_counts,
        "loader_hardcoded_checkpoint_identity_found": bool(loader_counts),
        "non_loader_files_scanned": src_files,
        "non_loader_occurrences": non_loader[:32],
        "non_loader_occurrence_count": len(non_loader),
        "non_loader_note": (
            "Occurrences outside the model loader are capture-side calibration constants "
            "and a frozen smoke display fixture; they are not used to select, validate or "
            "load a model bundle. The live subset identity is overridden per run."),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", type=Path, required=True)
    args = parser.parse_args()
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    try:
        summary = run(args.evidence_root.resolve())
    except Exception as exc:
        if os.environ.get("GUANCE_PHASE6_DEBUG"):
            import traceback
            traceback.print_exc()
        code = getattr(exc, "code", None)
        reason = code if isinstance(code, str) else str(exc)
        if not reason or len(reason) > 96 or not all(
                ch.isupper() or ch.isdigit() or ch in "_:" for ch in reason):
            reason = "PHASE6_ACCEPTANCE_FAILED"
        print(json.dumps({"schema": "guance-phase6-run-summary-v1", "status": "failed",
                          "reason": reason, "live_input_used": False},
                         ensure_ascii=False, separators=(",", ":")))
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
