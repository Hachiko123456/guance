"""Independent Phase 6 restart probe: catalog -> active -> resolve -> load -> recommend.

Every invocation is a fresh operating-system process that re-reads the active
selection document, re-validates the complete catalog (manifest, artifact bytes,
SHA-256, SHA256SUMS, provider contract), resolves the selected bundle, launches
the real JSONL model worker, loads the model, and requests one real
recommendation. Nothing is cached between restarts.

The probe only uses production classes (``ModelCatalog``, ``load_active_selection``,
``resolve_bundle``, ``default_client_factory``/``InferenceClient`` and the normal
worker). Output is bounded: identities, counters, device metadata and one
recommendation summary; never raw payloads, card ids, hidden hands or private
paths. Exit codes: 0 ready+recommendation, 2 fail-closed, 3 harness error.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT / "src"))
sys.dont_write_bytecode = True

from guance.contracts import (  # noqa: E402
    CanonicalPublicGameView, ConfirmedDecisionRecommendationRequest, GameObservation,
    PublicRecommendationRequest, RequestIdentity,
)
from guance.infrastructure.model.catalog import ModelCatalog  # noqa: E402
from guance.infrastructure.model.client import default_client_factory  # noqa: E402
from guance.infrastructure.model.errors import ModelBackendError  # noqa: E402
from guance.infrastructure.model.manifest import MAX_MANIFEST_BYTES, strict_json  # noqa: E402

TERMINAL = {"ready", "unavailable", "error", "timeout"}
_METADATA_FIELDS = (
    "status", "provider", "backend", "runtime", "runtime_version", "runtime_contract",
    "model_version", "iteration", "model_sha256", "bundle_fingerprint",
    "deployment_fingerprint", "classification", "score_kind", "wire_input_contract",
    "requested_device", "actual_device", "device_fallback", "device_reason",
    "torch_cuda_build", "experimental", "production_ready", "strength_claim",
    "legacy", "test_only",
)


def _wait(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _bounded_code(value: object, default: str = "MODEL_UNAVAILABLE") -> str:
    code = value if isinstance(value, str) else default
    if not code or len(code) > 64 or not all(ch.isupper() or ch.isdigit() or ch == "_" for ch in code):
        return default
    return code


def _selected_provider(source: Path) -> str:
    try:
        manifest = source / "manifest.json" if source.is_dir() else source
        raw = manifest.read_bytes()
        if len(raw) > MAX_MANIFEST_BYTES:
            return ""
        data = strict_json(raw.decode("utf-8"))
        value = data.get("provider") if isinstance(data, dict) else None
        return value if isinstance(value, str) and len(value) <= 128 else ""
    except Exception:
        return ""


def _metadata_summary(metadata: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in _METADATA_FIELDS:
        if name not in metadata:
            continue
        value = metadata[name]
        if isinstance(value, (bool, int, float)) or value is None:
            result[name] = value
        elif isinstance(value, str) and len(value) <= 256:
            result[name] = value
    capabilities = metadata.get("capabilities")
    if isinstance(capabilities, dict) and len(capabilities) <= 16:
        result["capabilities"] = {str(key): bool(value) for key, value in capabilities.items()
                                  if isinstance(key, str) and len(key) <= 64}
    return result


def _scan_summary(catalog: ModelCatalog) -> dict[str, Any]:
    try:
        scan = catalog.scan()
    except Exception as exc:
        return {"scan_error": _bounded_code(getattr(exc, "code", None), "CATALOG_BUNDLE_INVALID")}
    return {
        "entries": [
            {"model_id": entry.model_id, "directory": entry.directory,
             "provider": entry.provider, "runtime_contract": entry.runtime_contract}
            for entry in scan.entries
        ],
        "problems": [
            {"directory": problem.directory, "code": problem.code}
            for problem in scan.problems
        ],
    }


def _observation_payload(fixture: Path) -> tuple[GameObservation, CanonicalPublicGameView]:
    record = json.loads(fixture.read_text(encoding="utf-8"))[0]
    observation = GameObservation.from_dict(record)
    return observation, CanonicalPublicGameView.from_observation(observation)


def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    models_root = Path(args.models_root).resolve()
    output: dict[str, Any] = {
        "schema": "guance-phase6-restart-probe-v1",
        "pid": os.getpid(),
        "mode": args.mode,
        "requested_device": args.device,
        "status": "started",
        "fallback_suspected": False,
    }
    catalog = ModelCatalog(models_root / "catalog", models_root / "active_model.json",
                           root=models_root)
    output["catalog"] = _scan_summary(catalog)
    try:
        document = catalog.load_active()
    except ModelBackendError as exc:
        output.update(status="fail_closed", stage="active_load",
                      error_code=_bounded_code(exc.code, "ACTIVE_SELECTION_INVALID"))
        return output, 2
    except Exception:
        output.update(status="fail_closed", stage="active_load",
                      error_code="ACTIVE_SELECTION_INVALID")
        return output, 2
    if document is None:
        output.update(status="fail_closed", stage="active_load", error_code="NO_ACTIVE_SELECTION")
        return output, 2
    output["active"] = {"kind": document.kind, "model_id": document.model_id,
                        "bundle": document.bundle}
    if document.kind != "catalog":
        output.update(status="fail_closed", stage="active_resolve",
                      error_code="ACTIVE_SELECTION_INVALID")
        return output, 2
    try:
        source = catalog.resolve_bundle(document.model_id, document.bundle)
    except ModelBackendError as exc:
        output.update(status="fail_closed", stage="active_resolve",
                      error_code=_bounded_code(exc.code, "ACTIVE_SELECTION_INVALID"))
        return output, 2
    except Exception:
        output.update(status="fail_closed", stage="active_resolve",
                      error_code="ACTIVE_SELECTION_INVALID")
        return output, 2
    selected_provider = _selected_provider(source)
    output["selected_provider"] = selected_provider

    statuses: list[dict[str, Any]] = []
    results: list[Any] = []

    def on_status(status) -> None:
        statuses.append(status.to_dict())

    def on_result(result) -> None:
        results.append(result)

    client = default_client_factory(
        source, on_result=on_result, on_status=on_status,
        timeout_seconds=max(30.0, float(args.timeout)), threads=2,
        requested_device=args.device)
    try:
        client.start()
        if not _wait(lambda: bool(statuses) and statuses[-1].get("model_status") in TERMINAL,
                     float(args.load_timeout)):
            output.update(status="fail_closed", stage="load", error_code="MODEL_LOAD_TIMEOUT")
            return output, 2
        terminal = statuses[-1]
        output["load_status"] = terminal.get("model_status")
        metadata = {}
        for item in reversed(statuses):
            if isinstance(item.get("metadata"), dict) and item["metadata"]:
                metadata = dict(item["metadata"])
                break
        output["metadata"] = _metadata_summary(metadata)
        if terminal.get("model_status") != "ready":
            output["error_code"] = _bounded_code(terminal.get("error_code"),
                                                 "MODEL_LOAD_FAILED")
            output["status"] = "fail_closed"
            output["stage"] = "load"
            return output, 2

        if args.mode == "confirmed":
            payload = json.loads(Path(args.payload).read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                output.update(status="harness_error", stage="request", error_code="PAYLOAD_INVALID")
                return output, 3
            request = ConfirmedDecisionRecommendationRequest(
                RequestIdentity("", str(payload["session_id"]), str(payload["game_id"]),
                                int(payload["state_version"])), payload)
        else:
            _observation, view = _observation_payload(Path(args.fixture))
            request = PublicRecommendationRequest(
                RequestIdentity("", view.session_id, view.game_id, view.state_version), view)
        started = time.monotonic()
        request_id = client.submit(request, timeout_seconds=max(60.0, float(args.timeout)))
        if not isinstance(request_id, str) or not request_id:
            output.update(status="fail_closed", stage="recommend", error_code="MODEL_REQUEST_REJECTED")
            return output, 2
        if not _wait(lambda: bool(results) or (
                any(item.get("model_status") in {"error", "timeout"} for item in statuses[-4:])),
                max(120.0, float(args.timeout))):
            output.update(status="fail_closed", stage="recommend", error_code="MODEL_RECOMMENDATION_TIMEOUT")
            return output, 2
        if not results:
            code = terminal.get("error_code")
            for item in reversed(statuses[-4:]):
                if item.get("error_code"):
                    code = item["error_code"]
                    break
            output.update(status="fail_closed", stage="recommend",
                          error_code=_bounded_code(code, "MODEL_RECOMMENDATION_MISSING"))
            return output, 2
        result = results[-1]
        candidates = list(result.candidates)
        score = candidates[0].score if candidates else None
        output["recommendation"] = {
            "provider": str(result.provider),
            "model_version": str(result.model_version),
            "model_fingerprint": str(result.model_fingerprint),
            "score_kind": str(result.score_kind),
            "status": str(result.status),
            "candidate_count": len(candidates),
            "candidate_rank": int(candidates[0].rank) if candidates else 0,
            "candidate_kind": str(candidates[0].kind) if candidates else "",
            "cards_count": len(tuple(candidates[0].cards)) if candidates else 0,
            "score": float(score) if isinstance(score, (int, float)) else None,
            "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
            "result_elapsed_ms": float(result.elapsed_ms),
        }
        if (selected_provider and output["metadata"].get("provider")
                and output["metadata"]["provider"] != selected_provider) or (
                selected_provider and str(result.provider) != selected_provider):
            output["fallback_suspected"] = True
            output["status"] = "provider_mismatch"
            return output, 3
        output["status"] = "ready"
        return output, 0
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models-root", type=Path, required=True)
    parser.add_argument("--mode", choices=("public", "confirmed"), default="public")
    parser.add_argument("--payload", default="")
    parser.add_argument("--fixture", default=str(CODE_ROOT / "tests" / "fixtures" / "observations.json"))
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--load-timeout", type=float, default=600.0)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    try:
        output, code = run(args)
    except Exception as exc:  # bounded harness failure
        code_name = getattr(exc, "code", None)
        output = {"schema": "guance-phase6-restart-probe-v1", "pid": os.getpid(),
                  "mode": args.mode, "status": "harness_error",
                  "error_code": _bounded_code(code_name, "PROBE_HARNESS_ERROR"),
                  "fallback_suspected": False}
        code = 3
    text = json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    print(text)
    if args.out is not None:
        args.out.write_text(text + "\n", encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
