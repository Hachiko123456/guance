"""GPU-first inference acceptance runner for the 2026-10-20 task.

Modes are deliberately separate so evidence stays bounded and reproducible:

* ``fixed``      - canonical fixed input, full 34/32/32 regression on one device;
* ``smoke``      - real iter100/iter500/iter2000 load + one legal recommendation;
* ``no-gpu``     - real CPU-only-torch worker: auto->CPU, cuda->explicit failure;
* ``coldstart``  - independent worker cold starts (ready/device/identity/advice);
* ``numeric``    - in-process real-runtime probability/mask comparison data;
* ``compare``    - compare CPU and GPU numeric/fixed evidence (predeclared tolerance);
* ``perf``       - measured CPU/GPU latency, cold start, GPU memory and CUDA-event compute.

Every command writes bounded aggregate JSON (no cards, frames, payloads, hidden
hands, credentials or private absolute paths). ``live_input_used`` is always
false and ``recommendation_displayed`` is always false.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT))
sys.path.insert(0, str(CODE_ROOT / "src"))
sys.dont_write_bytecode = True

from scripts.run_phase5_offline_acceptance import (  # noqa: E402
    EXPECTED_BEST_NPZ_SHA256, EXPECTED_CAPTURE_BYTES, EXPECTED_CAPTURE_FRAMES,
    EXPECTED_CAPTURE_SHA256, EXPECTED_CHECKPOINT_BYTES, EXPECTED_CHECKPOINT_SHA256,
    EXPECTED_PROVIDER_ELIGIBLE, EXPECTED_RUNTIME_ENTRIES, EXPECTED_RUNTIME_ID,
    EXPECTED_SIDECAR_SHA256, EXPECTED_STRICT_ELIGIBLE, EXPECTED_STRICT_INELIGIBLE,
    MARVEL_SCORE_KIND, classify_decisions, replay_capture, validate_recommendation,
    validate_safe_output,
)
from guance.contracts import (  # noqa: E402
    ConfirmedDecisionRecommendationRequest, RequestIdentity,
)
from guance.infrastructure.model.client import InferenceClient  # noqa: E402
from guance.infrastructure.model.codec import dumps, envelope  # noqa: E402
from guance.infrastructure.model.errors import ModelBackendError  # noqa: E402
from guance.infrastructure.model.factory import ModelFactory  # noqa: E402
from guance.infrastructure.model.marvel_v2.action_protocol import ActionProtocolSession  # noqa: E402
from guance.infrastructure.model.marvel_v2.decision_request import (  # noqa: E402
    candidate_sequences_sha256, parse_confirmed_decision, project_confirmed_decision,
)
from guance.infrastructure.model.marvel_v2.encoder import encode_channels  # noqa: E402
from guance.infrastructure.model.marvel_v2.recommendation import _step_fields  # noqa: E402
from guance.infrastructure.model.marvel_v2.rules_oracle import (  # noqa: E402
    VerifiedGuandanRulesOracle,
)
from guance.public_state_contract import guandan_card_id_to_wire  # noqa: E402

PHASE2 = CODE_ROOT / "build" / "acceptance" / "guance-model-platform-20261016" / "phase2"
RUNTIME_ROOT = PHASE2 / "runtime" / "marvel-e5c82e7c-d7acbc8d"
RUNTIME_SOURCE = RUNTIME_ROOT / "source"
CHECKPOINT = Path(r"D:\project\guandan\runs\comparison_delivery\dataset\m09_candidate_iter002000.pt")
CAPTURE_ID = "20261014-143204"
CAPTURE = CODE_ROOT / "debug" / "captures" / CAPTURE_ID / "frames.jsonl"
SIDECAR = CODE_ROOT / "debug" / "captures" / CAPTURE_ID / "capture_status.json"
BEST_NPZ = CODE_ROOT / "data" / "profiles" / "tencent_daguandan" / "models" / "best.npz"
MODEL_VERSION = "MARVEL-V2-v1.3-iter002000"
PREDECLARED_TOLERANCE = {"rtol": 1e-3, "atol": 1e-4, "score_abs": 5e-3}
# Pre-declared numeric coverage: 4 numeric decisions, each with a 256-element
# probability/mask vector.  This is deliberately separate from the 32
# action/path-score records in the fixed-input evidence.
EXPECTED_NUMERIC_RECORDS = 4
EXPECTED_NUMERIC_VECTOR_LENGTH = 256
_ALLOWED_CODES = {
    "MODEL_INPUT_INVALID", "MODEL_INPUT_UNSUPPORTED", "MODEL_INFERENCE_FAILED",
    "MODEL_OUTPUT_INVALID", "MODEL_UNAVAILABLE", "MODEL_NOT_LOADED", "MODEL_LOAD_FAILED",
    "MODEL_TIMEOUT", "PROTOCOL_INVALID", "CONFIRMED_DECISION_UNAVAILABLE",
    "REQUEST_KIND_UNSUPPORTED", "RUNTIME_UNSUPPORTED", "MODEL_VERSION_UNSUPPORTED",
}


def _fail(code: str) -> None:
    raise RuntimeError(code)


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _hash_json(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False)
                          .encode("utf-8")).hexdigest()


def _bounded_code(value: object) -> str:
    code = value if isinstance(value, str) else "MODEL_INFERENCE_FAILED"
    return code if code in _ALLOWED_CODES else "MODEL_INFERENCE_FAILED"


def _failure_code(value: object) -> str:
    """Bounded harness code for progress/records (never carries payload data)."""
    code = getattr(value, "code", None)
    if isinstance(code, str):
        return _bounded_code(code)
    text = str(value) if isinstance(value, RuntimeError) else ""
    if text and text.replace("_", "").isalnum() and len(text) <= 64:
        return text
    return "MODEL_INFERENCE_FAILED"


def _write(path: Path, value: dict[str, Any]) -> None:
    validate_safe_output(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def _bundle_manifest(bundle_dir: Path) -> dict[str, Any]:
    return json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))


def _fixed_input_gate(bundle_dir: Path) -> dict[str, Any]:
    capture_sha, capture_bytes = _sha256_file(CAPTURE)
    sidecar_sha, _ = _sha256_file(SIDECAR)
    if (capture_sha, capture_bytes) != (EXPECTED_CAPTURE_SHA256, EXPECTED_CAPTURE_BYTES):
        _fail("CAPTURE_IDENTITY_MISMATCH")
    if sidecar_sha != EXPECTED_SIDECAR_SHA256:
        _fail("SIDECAR_IDENTITY_MISMATCH")
    if _line_count(CAPTURE) != EXPECTED_CAPTURE_FRAMES:
        _fail("CAPTURE_FRAME_COUNT_MISMATCH")
    runtime = json.loads((RUNTIME_ROOT / "runtime.json").read_text(encoding="utf-8"))
    if runtime.get("runtime_id") != EXPECTED_RUNTIME_ID:
        _fail("RUNTIME_IDENTITY_MISMATCH")
    entries = 0
    for line in (RUNTIME_ROOT / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, _, relative = line.partition("  ")
        target = RUNTIME_ROOT / relative
        if not target.is_file() or _sha256_file(target)[0] != digest:
            _fail("RUNTIME_SHA256SUMS_MISMATCH")
        entries += 1
    pycache = sum(1 for path in RUNTIME_ROOT.rglob("__pycache__") if path.is_dir())
    if entries != EXPECTED_RUNTIME_ENTRIES or pycache != 0:
        _fail("RUNTIME_INTEGRITY_INVALID")
    checkpoint_sha, checkpoint_bytes = _sha256_file(CHECKPOINT)
    if (checkpoint_sha, checkpoint_bytes) != (EXPECTED_CHECKPOINT_SHA256, EXPECTED_CHECKPOINT_BYTES):
        _fail("CHECKPOINT_IDENTITY_MISMATCH")
    if _sha256_file(BEST_NPZ)[0] != EXPECTED_BEST_NPZ_SHA256:
        _fail("FABLEDAN_WEIGHTS_MISMATCH")
    manifest = _bundle_manifest(bundle_dir)
    deployment_sha, deployment_bytes = _sha256_file(bundle_dir / "deployment.json")
    if (manifest.get("provider") != "guandan_marvel_v2"
            or manifest.get("runtime_contract") != "marvel_v2.device.v1"
            or manifest.get("artifact") != "deployment.json"
            or manifest.get("artifact_sha256") != deployment_sha
            or manifest.get("artifact_bytes") != deployment_bytes
            or manifest.get("classification") != "evaluation_only"
            or manifest.get("capabilities", {}).get("confirmed_decision") is not True
            or manifest.get("readiness", {}).get("production_ready") is not False
            or manifest.get("readiness", {}).get("strength_claim") is not False):
        _fail("BUNDLE_IDENTITY_MISMATCH")
    return {
        "schema": "guance-gpu-first-input-gate-v1",
        "provenance": "offline_fixed_inputs",
        "live_input_used": False,
        "capture_id": CAPTURE_ID,
        "capture_sha256": capture_sha,
        "capture_bytes": capture_bytes,
        "capture_frames": EXPECTED_CAPTURE_FRAMES,
        "capture_sidecar_sha256": sidecar_sha,
        "runtime_id": EXPECTED_RUNTIME_ID,
        "runtime_sha256sums_entries": entries,
        "runtime_pycache_directories": pycache,
        "checkpoint_sha256": checkpoint_sha,
        "checkpoint_bytes": checkpoint_bytes,
        "fabledan_weights_sha256": EXPECTED_BEST_NPZ_SHA256,
        "bundle_model_id": manifest.get("model_id"),
        "bundle_model_version": manifest.get("model_version"),
        "bundle_runtime_contract": manifest.get("runtime_contract"),
        "deployment_sha256": deployment_sha,
        "deployment_bytes": deployment_bytes,
    }


def _line_count(path: Path) -> int:
    count = 0
    with path.open("rb") as stream:
        for _ in stream:
            count += 1
    return count


def _prepare_fixed() -> tuple[dict[str, Any], dict[str, Any]]:
    replay = replay_capture()
    classification = classify_decisions(replay["decisions"])
    if (replay["provider_eligible"] != EXPECTED_PROVIDER_ELIGIBLE
            or len(classification["eligible"]) != EXPECTED_STRICT_ELIGIBLE
            or len(classification["ineligible"]) != EXPECTED_STRICT_INELIGIBLE):
        _fail("ELIGIBILITY_COUNT_MISMATCH")
    if not replay["capture_unchanged"]:
        _fail("CAPTURE_MUTATED_BY_REPLAY")
    return replay, classification


class _FrozenWorkerClient(InferenceClient):
    """Acceptance-only client that starts an already-built package EXE worker.

    It reuses the production client transport and environment hardening; only
    the interpreter command is replaced with the extracted package executable.
    """

    def __init__(self, *args: Any, executable: Path, **kwargs: Any) -> None:
        self._package_executable = str(Path(executable).resolve())
        super().__init__(*args, **kwargs)

    def _worker_process_spec(self) -> tuple[list[str], dict[str, str], int]:
        _command, env, flags = super()._worker_process_spec()
        return (
            [self._package_executable, "--model-worker", "--threads", str(self.threads),
             "--device", self.requested_device],
            env,
            flags,
        )


class _Worker:
    """Synchronous wrapper over the real production JSONL client."""

    def __init__(self, bundle_dir: Path, device: str, *, workload_timeout: float,
                 load_timeout: float = 180.0, executable: Path | None = None) -> None:
        self.statuses: list[dict[str, Any]] = []
        self.results: list[Any] = []
        self.metadata: dict[str, Any] = {}
        self.load_seconds = 0.0
        client_class = InferenceClient if executable is None else _FrozenWorkerClient
        extra = {} if executable is None else {"executable": Path(executable)}
        self.client = client_class(bundle_dir / "manifest.json", on_result=self.results.append,
                                   on_status=self._status, timeout_seconds=workload_timeout,
                                   threads=2, requested_device=device,
                                   load_timeout_seconds=load_timeout, **extra)

    def _status(self, status) -> None:
        item = status.to_dict()
        self.statuses.append(item)
        if item.get("metadata"):
            self.metadata = dict(item["metadata"])

    def start(self) -> dict[str, Any]:
        started = time.perf_counter()
        self.client.start()
        deadline = time.monotonic() + self.client.load_timeout
        while time.monotonic() < deadline:
            if any(item.get("model_status") in {"ready", "unavailable", "error", "timeout"}
                   for item in self.statuses):
                break
            time.sleep(0.05)
        self.load_seconds = time.perf_counter() - started
        if not self.statuses:
            _fail("MODEL_LOAD_TIMEOUT")
        final = self.statuses[-1]
        if final.get("model_status") != "ready":
            _fail(_bounded_code(final.get("error_code")))
        return final

    def submit(self, payload: dict[str, Any], *, timeout: float = 600.0) -> Any:
        count = len(self.results)
        request = ConfirmedDecisionRecommendationRequest(
            RequestIdentity("", payload["session_id"], payload["game_id"],
                            payload["state_version"]), payload)
        request_id = self.client.submit(request, timeout_seconds=timeout)
        if not isinstance(request_id, str) or not request_id:
            _fail("MODEL_REQUEST_REJECTED")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and len(self.results) <= count:
            if any(item.get("model_status") in {"error", "timeout"}
                   for item in self.statuses[-4:]):
                break
            time.sleep(0.02)
        if len(self.results) <= count:
            _fail("MODEL_RECOMMENDATION_MISSING")
        return self.results[-1]

    def close(self) -> None:
        self.client.close()

    def errors(self) -> dict[str, int]:
        codes: dict[str, int] = {}
        for item in self.statuses:
            if item.get("model_status") in {"error", "timeout"} and item.get("error_code"):
                code = _bounded_code(item["error_code"])
                codes[code] = codes.get(code, 0) + 1
        return dict(sorted(codes.items()))


def _action_record(result: Any, *, latency_ms: float | None = None) -> dict[str, Any]:
    candidate = result.candidates[0]
    signature = {
        "provider": result.provider, "model_version": result.model_version,
        "score_kind": result.score_kind, "kind": candidate.kind,
        "claim": list(candidate.claim or ()), "cards": list(candidate.cards),
    }
    record = {
        "action_sha256": _hash_json(signature),
        "kind": str(candidate.kind),
        "score_kind": str(candidate.score_kind),
        "model_version": result.model_version,
        "model_fingerprint": result.model_fingerprint,
        "ready": result.status == "ready" and not result.error_code,
    }
    if candidate.score is not None:
        record["path_score"] = float(candidate.score)
    if latency_ms is not None:
        record["latency_ms"] = round(latency_ms, 3)
    return record


def _device_summary(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        key: metadata.get(key) for key in
        ("requested_device", "actual_device", "device_fallback", "device_reason",
         "torch_cuda_build", "runtime", "runtime_contract", "model_version",
         "model_sha256", "iteration", "device_evidence")
        if metadata.get(key) not in (None, "")
    }


def run_fixed(bundle_dir: Path, device: str, output: Path, *, workload_timeout: float,
              executable: Path | None = None, packaged_env: bool = False,
              expect_device: str | None = None, progress: Path | None = None,
              load_timeout: float = 180.0) -> int:
    gate = _fixed_input_gate(bundle_dir)
    replay, classification = _prepare_fixed()
    oracle = VerifiedGuandanRulesOracle(RUNTIME_SOURCE)
    records = []
    saved_env = {}
    total = len(classification["eligible"])

    def emit_progress(event: dict[str, Any]) -> None:
        # Append-only coarse progress: request index, timing and status only.
        # Decision payloads never enter this file.
        if progress is None:
            return
        progress.parent.mkdir(parents=True, exist_ok=True)
        with progress.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()

    emit_progress({
        "event": "start", "requested_device": device, "expect_device": expect_device,
        "total_requests": total, "bundle_dir": str(bundle_dir),
        "worker_executable": str(executable) if executable is not None else "",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    })
    started_run = time.perf_counter()
    if packaged_env:
        for key in ("GUANCE_MARVEL_V2_SOURCE", "GUANCE_MARVEL_V2_CHECKPOINT"):
            saved_env[key] = os.environ.pop(key, None)
        scope = _env_scope(PYTHONDONTWRITEBYTECODE="1")
    else:
        scope = _env_scope(PYTHONDONTWRITEBYTECODE="1", GUANCE_MARVEL_V2_SOURCE=str(RUNTIME_SOURCE),
                           GUANCE_MARVEL_V2_CHECKPOINT=str(CHECKPOINT))
    try:
        with scope:
            worker = _Worker(bundle_dir, device, workload_timeout=workload_timeout,
                             executable=executable, load_timeout=load_timeout)
            try:
                final = worker.start()
                metadata = _device_summary(worker.metadata)
                emit_progress({
                    "event": "loaded", "elapsed_s": round(time.perf_counter() - started_run, 3),
                    "model_status": final.get("model_status"),
                    "actual_device": metadata.get("actual_device"),
                    "model_version": metadata.get("model_version"),
                    "model_sha256": metadata.get("model_sha256"),
                })
                if expect_device is not None and metadata.get("actual_device") != expect_device:
                    _fail("UNEXPECTED_DEVICE")
                if device == "cuda" and metadata.get("actual_device") != "cuda:0":
                    _fail("GPU_NOT_SELECTED")
                if device == "cpu" and metadata.get("actual_device") != "cpu":
                    _fail("CPU_NOT_SELECTED")
                seen: set[str] = set()
                for index, item in enumerate(classification["eligible"]):
                    context = item["context"]
                    payload = item["payload"]
                    if context.decision_identity in seen:
                        _fail("DUPLICATE_DECISION_IDENTITY")
                    seen.add(context.decision_identity)
                    before = _hash_json(payload)
                    started = time.perf_counter()
                    try:
                        result = worker.submit(payload)
                    except Exception as exc:  # noqa: BLE001 - bounded progress record
                        emit_progress({
                            "event": "request_failed", "index": index + 1, "total": total,
                            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
                            "cumulative_s": round(time.perf_counter() - started_run, 3),
                            "error": _failure_code(exc),
                        })
                        raise
                    elapsed = (time.perf_counter() - started) * 1000.0
                    if _hash_json(payload) != before or before != context.payload_hash:
                        _fail("MODEL_INPUT_MUTATED")
                    validated = validate_recommendation(context, result, oracle)
                    validated["action_sha256"] = _action_record(result)["action_sha256"]
                    validated["latency_ms"] = round(elapsed, 3)
                    records.append(validated)
                    emit_progress({
                        "event": "recommended", "index": index + 1, "total": total,
                        "elapsed_ms": validated["latency_ms"],
                        "cumulative_s": round(time.perf_counter() - started_run, 3),
                        "kind": validated["kind"],
                    })
                if worker.errors():
                    _fail("MODEL_ERRORS_PRESENT")
                fallback = int(metadata.get("device_fallback") is True)
            finally:
                worker.close()
    except Exception as exc:  # noqa: BLE001 - bounded final progress record
        emit_progress({
            "event": "finished", "status": "failed",
            "reason": _failure_code(exc),
            "recommendations": len(records), "total_requests": total,
            "last_success_index": len(records),
            "elapsed_s": round(time.perf_counter() - started_run, 3),
        })
        raise
    finally:
        for key, value in saved_env.items():
            if value is not None:
                os.environ[key] = value
    final_gate = _fixed_input_gate(bundle_dir)
    if final_gate != gate:
        _fail("INPUT_MUTATED_DURING_ACCEPTANCE")
    summary = {
        "schema": "guance-gpu-first-fixed-input-v1",
        "provenance": "offline_confirmed_decision",
        "live_input_used": False,
        "recommendation_displayed": False,
        "requested_device": device,
        "expect_device": expect_device,
        # Evidence carries identity, not host paths: the privacy contract used by
        # the acceptance writer rejects absolute paths, and the EXE hash plus name
        # already pin which binary produced the matrix.
        "worker_executable": Path(executable).name if executable is not None else "",
        "worker_executable_sha256": (
            _sha256_file(Path(executable))[0] if executable is not None else ""),
        "package_resolved_assets": packaged_env,
        "runtime": metadata,
        "load_seconds": round(worker.load_seconds, 3),
        "provider_eligible_count": replay["provider_eligible"],
        "strict_eligible_count": len(classification["eligible"]),
        "strict_ineligible_count": len(classification["ineligible"]),
        "recommendation_count": len(records),
        "recommendation_error_count": 0,
        "model_fallback_count": 0,
        "device_fallback": fallback,
        "input_immutable": True,
        "gate": gate,
        "records": records,
        "ineligible": [
            {"phase": item["phase"], "code": item["code"], "reason": item["reason"]}
            for item in classification["ineligible"]
        ],
    }
    emit_progress({
        "event": "finished", "status": "passed",
        "recommendations": len(records), "total_requests": total,
        "last_success_index": len(records),
        "elapsed_s": round(time.perf_counter() - started_run, 3),
    })
    _write(output, summary)
    print(json.dumps({k: summary[k] for k in (
        "requested_device", "runtime", "load_seconds", "provider_eligible_count",
        "strict_eligible_count", "recommendation_count", "device_fallback", "input_immutable")},
        ensure_ascii=False, sort_keys=True))
    return 0


def _validate_gpu_recommendation(context: Any, result: Any, oracle: Any) -> dict[str, Any]:
    """Same strict checks, but score/identity constants come from the bundle result."""
    if (result.session_id, result.game_id, result.state_version) != (
            context.session_id, context.game_id, context.state_version):
        _fail("RESULT_IDENTITY_MISMATCH")
    if result.status != "ready" or result.error_code or result.error_message:
        _fail("RESULT_NOT_READY")
    if result.provider != "guandan_marvel_v2" or result.score_kind != MARVEL_SCORE_KIND:
        _fail("RESULT_PROVIDER_MISMATCH")
    if len(result.candidates) != 1:
        _fail("RESULT_CANDIDATE_COUNT_INVALID")
    candidate = result.candidates[0]
    if candidate.rank != 1 or candidate.score_kind != MARVEL_SCORE_KIND:
        _fail("RESULT_CANDIDATE_INVALID")
    if candidate.score is None or float(candidate.score) > 0.0:
        _fail("RESULT_SCORE_INVALID")
    record = {"phase": context.phase, "kind": str(candidate.kind),
              "declaration_present": bool(candidate.claim), "valid": True}
    if candidate.kind == "pass":
        if tuple(candidate.cards) or not context.has_pass or not context.has_winning:
            _fail("RESULT_PASS_INVALID")
        record["family"] = "pass"
        return record
    cards = tuple(candidate.cards)
    if not cards or len(set(cards)) != len(cards) or not set(cards) <= set(context.own_hand_wire):
        _fail("RESULT_OWN_HAND_VIOLATION")
    if frozenset(cards) not in context.candidate_card_sets:
        _fail("RESULT_LEGAL_ROW_VIOLATION")
    claimed_kind = str(candidate.kind)
    matches = [item for item in context.candidates
               if item.declaration is not None
               and frozenset(guandan_card_id_to_wire(card) for card in item.physical_cards)
               == frozenset(cards)]
    if not matches:
        _fail("RESULT_DECLARATION_UNAVAILABLE")
    precise = [item for item in matches
               if _FAMILY_KIND.get(item.declaration.family) == claimed_kind]
    if not precise:
        _fail("RESULT_FAMILY_MISMATCH")
    for item in precise:
        item.declaration.validate(context.level)
        oracle.validate_resolved_play(item.declaration, wire_level=context.level,
                                      current=context.current_declaration)
    record["family"] = claimed_kind
    return record


_FAMILY_KIND = {
    "SINGLE": "single", "PAIR": "pair", "TRIPLE": "triple", "FULL_HOUSE": "full",
    "STRAIGHT": "straight", "STRAIGHT_FLUSH": "sflush", "PAIR_SEQUENCE": "serial_pair",
    "TRIPLE_SEQUENCE": "serial_triple", "RANK_BOMB": "bomb", "FOUR_KINGS": "rocket",
}


class _env_scope:
    def __init__(self, **values: str | None) -> None:
        self.values = values
        self.saved: dict[str, str | None] = {}

    def __enter__(self):
        for key, value in self.values.items():
            self.saved[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return self

    def __exit__(self, *_exc):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run_smoke(bundle_root: Path, device: str, output: Path, *, workload_timeout: float) -> int:
    import scripts.run_phase5_offline_acceptance as phase5
    records = []
    for bundle_dir in sorted(path.parent for path in bundle_root.glob("*-device/manifest.json")):
        manifest = _bundle_manifest(bundle_dir)
        deployment = json.loads((bundle_dir / "deployment.json").read_text(encoding="utf-8"))
        checkpoint_sha = deployment["checkpoint"]["sha256"]
        saved = (phase5.MODEL_VERSION, phase5.EXPECTED_CHECKPOINT_SHA256)
        try:
            phase5.MODEL_VERSION = manifest["model_version"]
            phase5.EXPECTED_CHECKPOINT_SHA256 = checkpoint_sha
            replay = phase5.replay_capture()
            classification = phase5.classify_decisions(replay["decisions"])
        finally:
            phase5.MODEL_VERSION, phase5.EXPECTED_CHECKPOINT_SHA256 = saved
        if (replay["provider_eligible"] != EXPECTED_PROVIDER_ELIGIBLE
                or len(classification["eligible"]) != EXPECTED_STRICT_ELIGIBLE):
            _fail("SMOKE_ELIGIBILITY_COUNT_MISMATCH")
        payload = classification["eligible"][0]["payload"]
        with _env_scope(PYTHONDONTWRITEBYTECODE="1", GUANCE_MARVEL_V2_SOURCE=str(RUNTIME_SOURCE),
                        GUANCE_MARVEL_V2_CHECKPOINT=str(_checkpoint_for(manifest["model_id"]))):
            worker = _Worker(bundle_dir, device, workload_timeout=workload_timeout)
            try:
                worker.start()
                metadata = _device_summary(worker.metadata)
                if metadata.get("model_sha256") != checkpoint_sha:
                    _fail("SMOKE_MODEL_IDENTITY_MISMATCH")
                result = worker.submit(payload)
                if result.status != "ready" or result.model_version != manifest["model_version"]:
                    _fail("SMOKE_RECOMMENDATION_INVALID")
                if result.model_fingerprint != checkpoint_sha:
                    _fail("SMOKE_FINGERPRINT_MISMATCH")
                record = _device_summary(worker.metadata)
                record.update(model_id=manifest["model_id"],
                              model_version=manifest["model_version"],
                              checkpoint_sha256=checkpoint_sha,
                              recommendation=_action_record(result))
                records.append(record)
            finally:
                worker.close()
    summary = {
        "schema": "guance-gpu-first-model-smoke-v1",
        "provenance": "offline_confirmed_decision",
        "live_input_used": False,
        "requested_device": device,
        "models": records,
    }
    _write(output, summary)
    print(json.dumps({"models": len(records), "devices": sorted({r.get("actual_device") for r in records})},
                     sort_keys=True))
    return 0


def _checkpoint_for(model_id: str) -> Path:
    mapping = {
        "marvel-v2-iter000100": Path(r"D:\project\guandan\runs\comparison_delivery\dataset"
                                     r"\m09_champion_iter000100.pt"),
        "marvel-v2-iter000500": Path(r"D:\project\guandan\runs\M09_full_training\remote"
                                     r"\iter500_formal_eval_20261014T030527Z\extracted"
                                     r"\candidates\ippo_iter000500.pt"),
        "marvel-v2-iter002000": CHECKPOINT,
    }
    try:
        return mapping[model_id]
    except KeyError:
        raise SystemExit("UNKNOWN_MODEL_ID") from None


def run_no_gpu(bundle_dir: Path, output: Path, *, workload_timeout: float) -> int:
    replay, classification = _prepare_fixed()
    payload = classification["eligible"][0]["payload"]
    records: dict[str, Any] = {}

    def attempt(requested: str) -> dict[str, Any]:
        with _env_scope(PYTHONDONTWRITEBYTECODE="1", GUANCE_MARVEL_V2_SOURCE=str(RUNTIME_SOURCE),
                        GUANCE_MARVEL_V2_CHECKPOINT=str(CHECKPOINT)):
            worker = _Worker(bundle_dir, requested, workload_timeout=workload_timeout)
            try:
                try:
                    worker.start()
                except RuntimeError as exc:
                    return {
                        "requested_device": requested,
                        "status": "failed",
                        "code": str(exc),
                        "device": _device_summary(worker.metadata),
                        "statuses": [{"model_status": item.get("model_status"),
                                      "error_code": _bounded_code(item.get("error_code"))}
                                     for item in worker.statuses],
                        "recommendation_count": 0,
                    }
                metadata = _device_summary(worker.metadata)
                result = worker.submit(payload)
                return {
                    "requested_device": requested,
                    "status": "ready",
                    "device": metadata,
                    "recommendation": _action_record(result),
                    "recommendation_count": 1,
                    "model_fallback_count": 0,
                }
            finally:
                worker.close()

    records["auto"] = attempt("auto")
    records["cuda"] = attempt("cuda")
    records["cpu"] = attempt("cpu")
    if records["auto"].get("status") != "ready":
        _fail("NO_GPU_AUTO_DID_NOT_USE_CPU")
    if records["auto"]["device"].get("actual_device") != "cpu":
        _fail("NO_GPU_AUTO_DEVICE_NOT_CPU")
    if records["auto"]["device"].get("device_reason") not in {"CPU_ONLY_TORCH", "NO_CUDA_DEVICE",
                                                             "CUDA_NOT_AVAILABLE",
                                                             "CUDA_INIT_UNAVAILABLE"}:
        _fail("NO_GPU_AUTO_REASON_INVALID")
    if records["cuda"].get("status") != "failed":
        _fail("NO_GPU_CUDA_DID_NOT_FAIL")
    if records["cuda"].get("code") != "MODEL_UNAVAILABLE":
        _fail("NO_GPU_CUDA_FAILURE_CODE_INVALID")
    if records["cpu"].get("status") != "ready":
        _fail("NO_GPU_FORCED_CPU_FAILED")
    summary = {
        "schema": "guance-gpu-first-no-gpu-worker-v1",
        "provenance": "real_process_cpu_only_torch",
        "live_input_used": False,
        "cuda_device_mask_used": False,
        "controlled_environment": "accepted CPU-only Torch interpreter (real process)",
        "results": records,
    }
    _write(output, summary)
    print(json.dumps({name: {"status": value.get("status"),
                             "device": value.get("device", {}).get("actual_device"),
                             "reason": value.get("device", {}).get("device_reason"),
                             "code": value.get("code")}
                      for name, value in records.items()}, ensure_ascii=False, sort_keys=True))
    return 0


def run_coldstart(bundle_dir: Path, device: str, output: Path, runs: int,
                  *, workload_timeout: float) -> int:
    replay, classification = _prepare_fixed()
    payload = classification["eligible"][0]["payload"]
    records = []
    for index in range(1, runs + 1):
        record: dict[str, Any] = {"run": index, "requested_device": device}
        try:
            with _env_scope(PYTHONDONTWRITEBYTECODE="1", GUANCE_MARVEL_V2_SOURCE=str(RUNTIME_SOURCE),
                            GUANCE_MARVEL_V2_CHECKPOINT=str(CHECKPOINT)):
                worker = _Worker(bundle_dir, device, workload_timeout=workload_timeout)
                try:
                    started = time.perf_counter()
                    worker.start()
                    record["load_seconds"] = round(time.perf_counter() - started, 3)
                    record["status"] = "ready"
                    record["device"] = _device_summary(worker.metadata)
                    submit_started = time.perf_counter()
                    result = worker.submit(payload)
                    record["recommendation_seconds"] = round(
                        time.perf_counter() - submit_started, 3)
                    record["recommendation"] = _action_record(result)
                finally:
                    worker.close()
        except Exception as exc:
            record["status"] = "failed"
            record["code"] = _bounded_code(getattr(exc, "code", exc))
            records.append(record)
            _write(output, {"schema": "guance-gpu-first-cold-start-v1",
                            "requested_device": device, "runs_requested": runs,
                            "runs": records, "first_failure_preserved": True,
                            "silent_retry": False})
            raise SystemExit(f"COLD_START_FAILED:{record['code']}") from None
        records.append(record)
    summary = {
        "schema": "guance-gpu-first-cold-start-v1",
        "requested_device": device,
        "runs_requested": runs,
        "runs_completed": len(records),
        "first_failure_preserved": False,
        "silent_retry": False,
        "runs": records,
    }
    _write(output, summary)
    print(json.dumps({"runs": len(records), "load_seconds": [r["load_seconds"] for r in records],
                      "devices": [r.get("device", {}).get("actual_device") for r in records]},
                     sort_keys=True))
    return 0


def _step_sequence(parsed: Any, device: str, torch: Any) -> list[dict[str, Any]]:
    oracle = VerifiedGuandanRulesOracle(RUNTIME_SOURCE)
    own_hand_wire = tuple(guandan_card_id_to_wire(card) for card in parsed.own_hand)
    candidates = oracle.legal_action_candidates(
        own_hand_wire, wire_level=parsed.level, phase=parsed.phase,
        current=parsed.current_declaration)
    if candidate_sequences_sha256(candidates) != parsed.candidates_sha256:
        _fail("NUMERIC_CANDIDATE_MISMATCH")
    session = ActionProtocolSession(
        parsed.session_id, parsed.state_version, parsed.phase, parsed.own_hand, candidates,
        confirmed_domain=None, prefix=(), token_step=parsed.token_step,
        committed_step=parsed.committed_step)
    rows = session.legal_rows
    observer = parsed.marvel_state.observer_seat
    channels = encode_channels(parsed.marvel_state)
    return [
        _step_fields(step.rows, observer=observer, channels=channels, legal_rows=rows,
                     torch=torch, device=device, current=index + 1 == len(parsed.history.steps))
        for index, step in enumerate(parsed.history.steps)
    ]


def run_numeric(bundle_dir: Path, device: str, output: Path, *, limit: int) -> int:
    replay, classification = _prepare_fixed()
    records = []
    factory = ModelFactory(requested_device=device)
    advisor = factory.create(bundle_dir)
    try:
        metadata = advisor.load().to_dict()
        runtime = advisor.runtime._runtime
        torch = runtime._torch
        if metadata.get("actual_device", "").startswith("cuda:"):
            torch.cuda.reset_peak_memory_stats()
        selected = classification["eligible"][:limit]
        events = []
        for item in selected:
            parsed = parse_confirmed_decision(item["payload"])
            steps = _step_sequence(parsed, metadata["actual_device"], torch)
            if torch.cuda.is_available() and metadata["actual_device"].startswith("cuda:"):
                start = torch.cuda.Event(enable_timing=True)
                end = torch.cuda.Event(enable_timing=True)
                start.record()
                probabilities, _cache = runtime.forward_confirmed_stepwise(
                    steps, timeout_seconds=300)
                end.record()
                end.synchronize()
                events.append(start.elapsed_time(end))
            else:
                started = time.perf_counter()
                probabilities, _cache = runtime.forward_confirmed_stepwise(
                    steps, timeout_seconds=300)
                events.append((time.perf_counter() - started) * 1000.0)
            row = probabilities.detach().to("cpu").tolist()
            weights = steps[-1]["avail_weights"][0, -1].detach().to("cpu").tolist()
            mask = [1 if weight > 0 else 0 for weight in weights]
            argmax = max(range(len(row)), key=lambda index: row[index])
            records.append({
                "decision_identity": item["context"].decision_identity,
                "phase": item["context"].phase,
                "probabilities": [round(value, 10) for value in row],
                "mask": mask,
                "argmax": argmax,
                "argmax_probability": row[argmax],
                "sum": sum(row),
                "compute_ms": round(events[-1], 3),
            })
        peak = None
        if metadata.get("actual_device", "").startswith("cuda:"):
            peak = int(torch.cuda.max_memory_allocated())
    finally:
        advisor.close()
    summary = {
        "schema": "guance-gpu-first-numeric-probe-v1",
        "requested_device": device,
        "runtime": _device_summary(metadata),
        "tolerance": PREDECLARED_TOLERANCE,
        "gpu_peak_memory_bytes": peak,
        "records": records,
    }
    _write(output, summary)
    print(json.dumps({"records": len(records), "device": metadata.get("actual_device"),
                      "peak": peak}, sort_keys=True))
    return 0


def _finite_number(value: object, code: str) -> float:
    """Accept only real finite numbers; bools, strings, NaN and Inf are rejected."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(code)
    number = float(value)
    if not math.isfinite(number):
        _fail(code)
    return number


def _record_list(payload: object, *, empty_code: str, count_code: str,
                 expected_count: int) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        _fail("COMPARE_MALFORMED")
    records = payload.get("records")
    if not isinstance(records, list):
        _fail("COMPARE_MALFORMED")
    if not records:
        _fail(empty_code)
    if len(records) != expected_count:
        _fail(count_code)
    if any(not isinstance(record, dict) for record in records):
        _fail("COMPARE_MALFORMED")
    return records


def _unique_identities(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    identities: list[str] = []
    for record in records:
        identity = record.get("decision_identity")
        if not isinstance(identity, str) or not identity:
            _fail("COMPARE_MALFORMED")
        identities.append(identity)
    if len(set(identities)) != len(identities):
        _fail("COMPARE_DUPLICATE_IDENTITY")
    return {record["decision_identity"]: record for record in records}


def _element_within_tolerance(cpu_value: float, gpu_value: float,
                              tolerance: dict[str, float]) -> bool:
    """Pre-declared elementwise rule: atol + rtol * max(|cpu|, |gpu|).

    The old rule used ``rtol * 1.0``, which degraded to a fixed ~1.1e-3
    absolute threshold and silently accepted large relative drift of small
    probabilities.  This rule keeps the relative term proportional to the
    compared values.
    """
    difference = abs(cpu_value - gpu_value)
    scale = max(abs(cpu_value), abs(gpu_value))
    return difference <= tolerance["atol"] + tolerance["rtol"] * scale


def _compare_action_records(cpu: dict[str, Any], gpu: dict[str, Any],
                            tolerance: dict[str, float]) -> tuple[dict[str, Any], dict[str, Any]]:
    cpu_records = _record_list(cpu, empty_code="COMPARE_ACTION_EMPTY",
                               count_code="COMPARE_ACTION_COUNT",
                               expected_count=EXPECTED_STRICT_ELIGIBLE)
    gpu_records = _record_list(gpu, empty_code="COMPARE_ACTION_EMPTY",
                               count_code="COMPARE_ACTION_COUNT",
                               expected_count=EXPECTED_STRICT_ELIGIBLE)
    for payload in (cpu, gpu):
        if payload.get("recommendation_count") != EXPECTED_STRICT_ELIGIBLE:
            _fail("COMPARE_RECOMMENDATION_COUNT")
    cpu_by_id = _unique_identities(cpu_records)
    gpu_by_id = _unique_identities(gpu_records)
    if set(cpu_by_id) != set(gpu_by_id):
        _fail("COMPARE_IDENTITY_SET_MISMATCH")
    action_differences: list[dict[str, Any]] = []
    score_differences: list[float] = []
    for identity in sorted(cpu_by_id):
        left, right = cpu_by_id[identity], gpu_by_id[identity]
        left_action = left.get("action_sha256")
        right_action = right.get("action_sha256")
        if not isinstance(left_action, str) or not isinstance(right_action, str):
            _fail("COMPARE_MALFORMED")
        if left_action != right_action:
            action_differences.append({"decision_identity": identity,
                                       "cpu_kind": left.get("kind"),
                                       "gpu_kind": right.get("kind")})
        left_score = _finite_number(left.get("path_score"), "COMPARE_NONFINITE")
        right_score = _finite_number(right.get("path_score"), "COMPARE_NONFINITE")
        score_differences.append(abs(left_score - right_score))
    max_score = max(score_differences)
    if action_differences:
        _fail("COMPARE_ACTION_DIFFERENCE")
    if max_score > tolerance["score_abs"]:
        _fail("COMPARE_SCORE_TOLERANCE")
    return ({
        "record_count": len(cpu_by_id),
        "score_kind": "path_score",
        "max_path_score_abs_difference": max_score,
        "score_within_tolerance": True,
        "action_differences": action_differences,
        "action_identical": True,
    }, {"max_path_score_abs_difference": max_score})


def _compare_numeric_records(numeric_cpu: dict[str, Any], numeric_gpu: dict[str, Any],
                             tolerance: dict[str, float]) -> dict[str, Any]:
    cpu_records = _record_list(numeric_cpu, empty_code="COMPARE_NUMERIC_EMPTY",
                               count_code="COMPARE_NUMERIC_COUNT",
                               expected_count=EXPECTED_NUMERIC_RECORDS)
    gpu_records = _record_list(numeric_gpu, empty_code="COMPARE_NUMERIC_EMPTY",
                               count_code="COMPARE_NUMERIC_COUNT",
                               expected_count=EXPECTED_NUMERIC_RECORDS)
    cpu_by_id = _unique_identities(cpu_records)
    gpu_by_id = _unique_identities(gpu_records)
    if set(cpu_by_id) != set(gpu_by_id):
        _fail("COMPARE_IDENTITY_SET_MISMATCH")

    max_abs = 0.0
    max_rel = 0.0
    element_count = 0
    element_violations = 0
    for identity in sorted(cpu_by_id):
        left, right = cpu_by_id[identity], gpu_by_id[identity]
        left_probabilities = left.get("probabilities")
        right_probabilities = right.get("probabilities")
        left_mask = left.get("mask")
        right_mask = right.get("mask")
        if not isinstance(left_probabilities, list) or not isinstance(right_probabilities, list) \
                or not isinstance(left_mask, list) or not isinstance(right_mask, list):
            _fail("COMPARE_MALFORMED")
        if len(left_probabilities) != len(left_mask) \
                or len(right_probabilities) != len(right_mask) \
                or len(left_probabilities) != len(right_probabilities):
            _fail("COMPARE_VECTOR_LENGTH")
        if len(left_probabilities) != EXPECTED_NUMERIC_VECTOR_LENGTH:
            _fail("COMPARE_VECTOR_LENGTH")
        for value in left_probabilities:
            _finite_number(value, "COMPARE_NONFINITE")
        for value in right_probabilities:
            _finite_number(value, "COMPARE_NONFINITE")
        for key in ("argmax_probability", "sum"):
            if key in left:
                _finite_number(left[key], "COMPARE_NONFINITE")
            if key in right:
                _finite_number(right[key], "COMPARE_NONFINITE")
        for mask in (left_mask, right_mask):
            if any(isinstance(item, bool) or not isinstance(item, int) or item not in (0, 1)
                   for item in mask):
                _fail("COMPARE_MALFORMED")
        left_argmax = left.get("argmax")
        right_argmax = right.get("argmax")
        if isinstance(left_argmax, bool) or not isinstance(left_argmax, int) \
                or isinstance(right_argmax, bool) or not isinstance(right_argmax, int):
            _fail("COMPARE_MALFORMED")
        if not 0 <= left_argmax < len(left_probabilities) \
                or not 0 <= right_argmax < len(right_probabilities):
            _fail("COMPARE_ARGMAX_INVALID")
        if left_mask != right_mask:
            _fail("COMPARE_MASK_MISMATCH")
        if left_argmax != right_argmax:
            _fail("COMPARE_ARGMAX_MISMATCH")
        for cpu_value, gpu_value in zip(left_probabilities, right_probabilities):
            cpu_number = float(cpu_value)
            gpu_number = float(gpu_value)
            difference = abs(cpu_number - gpu_number)
            scale = max(abs(cpu_number), abs(gpu_number))
            max_abs = max(max_abs, difference)
            if scale != 0.0:
                max_rel = max(max_rel, difference / scale)
            element_count += 1
            if not _element_within_tolerance(cpu_number, gpu_number, tolerance):
                element_violations += 1
    if element_violations:
        _fail("COMPARE_PROBABILITY_TOLERANCE")
    return {
        "record_count": len(cpu_by_id),
        "element_count": element_count,
        "vector_length": EXPECTED_NUMERIC_VECTOR_LENGTH,
        "max_probability_abs_difference": max_abs,
        "max_probability_rel_difference": max_rel,
        "probability_within_tolerance": True,
        "element_violations": 0,
        "mask_mismatches": 0,
        "argmax_mismatches": 0,
    }


def run_compare(cpu_path: Path, gpu_path: Path, numeric_cpu: Path, numeric_gpu: Path,
                output: Path) -> int:
    cpu = json.loads(cpu_path.read_text(encoding="utf-8"))
    gpu = json.loads(gpu_path.read_text(encoding="utf-8"))
    numeric_cpu_data = json.loads(numeric_cpu.read_text(encoding="utf-8"))
    numeric_gpu_data = json.loads(numeric_gpu.read_text(encoding="utf-8"))
    tolerance = PREDECLARED_TOLERANCE
    action_comparison, _ = _compare_action_records(cpu, gpu, tolerance)
    probability_comparison = _compare_numeric_records(
        numeric_cpu_data, numeric_gpu_data, tolerance)
    summary = {
        "schema": "guance-gpu-first-numeric-parity-repair1-v2",
        "tolerance": tolerance,
        "probability_element_rule": (
            "abs(cpu-gpu) <= atol + rtol * max(abs(cpu), abs(gpu))"),
        "cpu_requested_device": cpu.get("requested_device"),
        "gpu_requested_device": gpu.get("requested_device"),
        "declared_coverage": {
            "action_path_score_records": EXPECTED_STRICT_ELIGIBLE,
            "numeric_probability_mask_records": EXPECTED_NUMERIC_RECORDS,
            "numeric_vector_length": EXPECTED_NUMERIC_VECTOR_LENGTH,
        },
        "action_record_count": action_comparison["record_count"],
        "numeric_record_count": probability_comparison["record_count"],
        "numeric_element_count": probability_comparison["element_count"],
        "action_comparison": action_comparison,
        "probability_comparison": probability_comparison,
        "action_identical": action_comparison["action_identical"],
        "max_path_score_abs_difference": action_comparison["max_path_score_abs_difference"],
        "score_within_tolerance": action_comparison["score_within_tolerance"],
        "max_probability_abs_difference": probability_comparison[
            "max_probability_abs_difference"],
        "max_probability_rel_difference": probability_comparison[
            "max_probability_rel_difference"],
        "probability_within_tolerance": probability_comparison[
            "probability_within_tolerance"],
        "mask_mismatches": probability_comparison["mask_mismatches"],
        "argmax_mismatches": probability_comparison["argmax_mismatches"],
        "declared_tolerance_source": "acceptance-mapping.json predeclared_numeric_tolerance",
        "explanation_required_for_action_differences": True,
    }
    _write(output, summary)
    print(json.dumps({key: summary[key] for key in (
        "action_record_count", "numeric_record_count", "numeric_element_count",
        "action_identical", "max_path_score_abs_difference",
        "max_probability_abs_difference", "mask_mismatches", "argmax_mismatches")},
        sort_keys=True))
    return 0


def run_perf(bundle_dir: Path, device: str, output: Path, *, repetitions: int, limit: int,
             workload_timeout: float) -> int:
    replay, classification = _prepare_fixed()
    selected = classification["eligible"][:limit]
    with _env_scope(PYTHONDONTWRITEBYTECODE="1", GUANCE_MARVEL_V2_SOURCE=str(RUNTIME_SOURCE),
                    GUANCE_MARVEL_V2_CHECKPOINT=str(CHECKPOINT)):
        worker = _Worker(bundle_dir, device, workload_timeout=workload_timeout)
        try:
            worker.start()
            metadata = _device_summary(worker.metadata)
            first_started = time.perf_counter()
            worker.submit(selected[0]["payload"])
            first_seconds = time.perf_counter() - first_started
            worker.submit(selected[0]["payload"])  # warmup, result discarded
            repetitions_data = []
            for _ in range(repetitions):
                started = time.perf_counter()
                for item in selected:
                    worker.submit(item["payload"])
                repetitions_data.append((time.perf_counter() - started) / len(selected))
        finally:
            worker.close()
    ordered = sorted(repetitions_data)
    # Each sample is the *mean* seconds per recommendation inside one warm
    # round.  With only three rounds this is not a per-request tail latency;
    # do not label its maximum as a per-request P95.
    summary = {
        "schema": "guance-gpu-first-performance-repair1-v2",
        "requested_device": device,
        "runtime": metadata,
        "load_seconds": round(worker.load_seconds, 3),
        "first_recommendation_seconds": round(first_seconds, 3),
        "warm_repetitions": repetitions,
        "sample_count": len(selected),
        "warm_round_seconds_per_recommendation": {
            "median": round(statistics.median(ordered), 3),
            "max": round(max(ordered), 3),
            "samples": [round(value, 3) for value in repetitions_data],
            "sample_unit": (
                "mean seconds per recommendation within one warm round; "
                "each round covers sample_count decisions"),
            "per_request_p95": "UNVERIFIED",
            "per_request_p95_note": (
                "only warm_repetitions round means were recorded; per-request "
                "tail latency was not measured"),
        },
        "measurement": "end-to-end production client/worker latency; not GPU kernel time",
    }
    _write(output, summary)
    print(json.dumps({key: summary[key] for key in (
        "requested_device", "load_seconds", "first_recommendation_seconds",
        "warm_round_seconds_per_recommendation")}, sort_keys=True))
    return 0


def _recompute_perf_side(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    warm = data.get("warm_round_seconds_per_recommendation") \
        or data.get("warm_seconds_per_recommendation")
    if not isinstance(warm, dict) or not isinstance(warm.get("samples"), list) \
            or not warm["samples"]:
        _fail("PERF_SAMPLES_MISSING")
    samples = [_finite_number(value, "PERF_NONFINITE") for value in warm["samples"]]
    source_sha, source_bytes = _sha256_file(path)
    return {
        "source_file": path.name,
        "source_bytes": source_bytes,
        "source_sha256": source_sha,
        "requested_device": data.get("requested_device"),
        "runtime_actual_device": (data.get("runtime") or {}).get("actual_device"),
        "torch_cuda_build": (data.get("runtime") or {}).get("torch_cuda_build"),
        "load_seconds": data.get("load_seconds"),
        "first_recommendation_seconds": data.get("first_recommendation_seconds"),
        "sample_count_per_round": data.get("sample_count"),
        "warm_rounds": len(samples),
        "round_mean_seconds_per_recommendation": {
            "samples": samples,
            "median": round(statistics.median(samples), 3),
            "max": round(max(samples), 3),
        },
        "per_request_p95": "UNVERIFIED",
        "per_request_p95_note": (
            "only round means were recorded; per-request tail latency was not measured"),
        "unit": "mean seconds per recommendation within one warm round",
    }


def run_recompute_perf(cpu_path: Path, gpu_path: Path, output: Path) -> int:
    cpu = _recompute_perf_side(cpu_path)
    gpu = _recompute_perf_side(gpu_path)
    if cpu["runtime_actual_device"] != "cpu" or gpu["runtime_actual_device"] != "cuda:0":
        _fail("PERF_DEVICE_MISMATCH")
    summary = {
        "schema": "guance-gpu-first-performance-recomputed-repair1-v1",
        "source_files_read_only": True,
        "benchmark_rerun": False,
        "algorithm": (
            "read the stored warm samples; each sample is the mean seconds per "
            "recommendation inside one warm round of sample_count decisions; "
            "report median and max of those round means; do not call the max a "
            "per-request P95"),
        "cpu": cpu,
        "gpu": gpu,
        "cpu_environment_classification": (
            "the source performance-cpu.json was produced by the cu126 build "
            "with --device cpu, not by the CPU-only torch build"),
        "perf_1_correction": (
            "PERF-1 previously quoted CPU 4.84 s and called the maximum round "
            "mean a per-request P95; corrected values are the medians/maxima "
            "in this record, and per-request P95 remains UNVERIFIED"),
    }
    _write(output, summary)
    print(json.dumps({
        "cpu_median": cpu["round_mean_seconds_per_recommendation"]["median"],
        "cpu_max": cpu["round_mean_seconds_per_recommendation"]["max"],
        "gpu_median": gpu["round_mean_seconds_per_recommendation"]["median"],
        "gpu_max": gpu["round_mean_seconds_per_recommendation"]["max"],
        "per_request_p95": "UNVERIFIED",
    }, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(name: str):
        item = sub.add_parser(name)
        item.add_argument("--bundle-dir", type=Path, required=True)
        item.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
        item.add_argument("--output", type=Path, required=True)
        item.add_argument("--workload-timeout", type=float, default=600.0)
        return item

    fixed = common("fixed")
    fixed.add_argument("--worker-executable", type=Path, default=None,
                       help="run the real JSONL worker from an extracted package EXE")
    fixed.add_argument("--packaged-env", action="store_true",
                       help="clear MARVEL source/checkpoint env so the package must self-locate")
    fixed.add_argument("--expect-device", choices=("cpu", "cuda:0"), default=None)
    fixed.add_argument("--progress", type=Path, default=None,
                       help="append coarse per-request progress JSONL (no decision payloads)")
    fixed.set_defaults(func=lambda args: run_fixed(args.bundle_dir, args.device, args.output,
                                                   workload_timeout=args.workload_timeout,
                                                   executable=args.worker_executable,
                                                   packaged_env=args.packaged_env,
                                                   expect_device=args.expect_device,
                                                   progress=args.progress))
    smoke = common("smoke")
    smoke.set_defaults(func=lambda args: run_smoke(args.bundle_dir.parent, args.device,
                                                   args.output,
                                                   workload_timeout=args.workload_timeout))
    no_gpu = sub.add_parser("no-gpu")
    no_gpu.add_argument("--bundle-dir", type=Path, required=True)
    no_gpu.add_argument("--output", type=Path, required=True)
    no_gpu.add_argument("--workload-timeout", type=float, default=600.0)
    no_gpu.set_defaults(func=lambda args: run_no_gpu(args.bundle_dir, args.output,
                                                     workload_timeout=args.workload_timeout))
    cold = common("coldstart")
    cold.add_argument("--runs", type=int, default=3)
    cold.set_defaults(func=lambda args: run_coldstart(args.bundle_dir, args.device, args.output,
                                                      args.runs,
                                                      workload_timeout=args.workload_timeout))
    numeric = common("numeric")
    numeric.add_argument("--limit", type=int, default=4)
    numeric.set_defaults(func=lambda args: run_numeric(args.bundle_dir, args.device, args.output,
                                                       limit=args.limit))
    compare = sub.add_parser("compare")
    compare.add_argument("--cpu", type=Path, required=True)
    compare.add_argument("--gpu", type=Path, required=True)
    compare.add_argument("--numeric-cpu", type=Path, required=True)
    compare.add_argument("--numeric-gpu", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)
    compare.set_defaults(func=lambda args: run_compare(args.cpu, args.gpu, args.numeric_cpu,
                                                       args.numeric_gpu, args.output))
    perf = common("perf")
    perf.add_argument("--repetitions", type=int, default=3)
    perf.add_argument("--limit", type=int, default=5)
    perf.set_defaults(func=lambda args: run_perf(args.bundle_dir, args.device, args.output,
                                                 repetitions=args.repetitions, limit=args.limit,
                                                 workload_timeout=args.workload_timeout))
    recompute = sub.add_parser("recompute-perf")
    recompute.add_argument("--cpu", type=Path, required=True)
    recompute.add_argument("--gpu", type=Path, required=True)
    recompute.add_argument("--output", type=Path, required=True)
    recompute.set_defaults(func=lambda args: run_recompute_perf(args.cpu, args.gpu,
                                                                args.output))
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except SystemExit:
        raise
    except Exception as exc:
        code = _bounded_code(getattr(exc, "code", exc))
        print(json.dumps({"status": "failed", "reason": code}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
