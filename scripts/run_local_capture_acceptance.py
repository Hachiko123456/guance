"""Read-only acceptance for the five explicitly selected legacy captures.

The command emits one bounded JSON summary. It never writes beside a capture,
serializes a converted decision, or prints source/model input contents.

All fixed inputs are frozen and hash-pinned: the rules/runtime tree is the Phase 2
materialized runtime source (never the live Guandan worktree), the bundle is the
Phase 2 current-schema ``marvel-v2-iter002000`` deployment descriptor, and the
checkpoint is the fixed iter2000 artifact. The five legacy captures are fixed,
read-only, deterministic negatives (``event_unsupported``); the model runtime
loads fail-closed to ``unavailable`` and produces no recommendation.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Iterable

CODE_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = CODE_ROOT / "tests" / "local_capture_manifest.json"
TASK_ID = "GMV2-P7-LOCAL-CAPTURE-ACCEPTANCE-20261013"
SCHEMA = "guance-gmv2-phase7-local-capture-summary-v1"
_PHASE2 = (CODE_ROOT / "build" / "acceptance"
           / "guance-model-platform-20261016" / "phase2")
CHECKPOINT_SHA256 = "af0df1eaeb67c88e467ee78bf6b0494c49d000f5733d655ee81b40351d4ca9c8"
CHECKPOINT_BYTES = 8_588_181
# Frozen Phase 2 materialized runtime source; the live Guandan worktree is never
# used as a trusted rules/runtime root.
EXPECTED_MODEL_SOURCE = _PHASE2 / "runtime" / "marvel-e5c82e7c-d7acbc8d" / "source"
EXPECTED_CHECKPOINT = Path(
    r"D:\project\guandan\runs\comparison_delivery\dataset\m09_candidate_iter002000.pt")
EXPECTED_MODEL_MANIFEST = _PHASE2 / "bundles" / "marvel-v2-iter002000" / "manifest.json"
_ALLOWED_SELECTOR_KEYS = {
    "fixture_id", "capture_id", "relative_path", "source_sha256", "expected_level",
    "expected_observer_seat", "expected_decision_count", "expected_quality", "status",
}
_ALLOWED_UNSUPPORTED = {
    "CAPTURE_CLOSED_SIDECAR_MISSING",
    "CAPTURE_CLOSED_SIDECAR_INVALID",
    "CAPTURE_PROTOCOL_REJECTED",
    "LOCAL_CAPTURE_CONVERSION_REJECTED",
    "LOCAL_CAPTURE_DECISION_COUNT_MISMATCH",
    "LOCAL_CAPTURE_DECISIONS_UNAVAILABLE",
    "LOCAL_CAPTURE_PRIVACY_CONTRACT_INVALID",
    "LOCAL_CAPTURE_SELECTOR_INVALID",
    "LOCAL_CAPTURE_SETTLEMENT_UNVERIFIED",
    "MARVEL_COUNTERS_UNAVAILABLE",
    "MODEL_INPUT_AMBIGUOUS",
    "MODEL_INPUT_INVALID",
    "MODEL_INPUT_UNSUPPORTED",
    "MODEL_OUTPUT_UNVERIFIED",
    "MODEL_UNAVAILABLE",
    "MODEL_VERSION_UNSUPPORTED",
    "GUANDAN_STATE_UNAVAILABLE",
}
_FORBIDDEN_MODEL_KEYS = {
    "authorization", "cookie", "cookies", "ca_private_key", "raw", "raw_data",
    "request_body", "response_body", "post_state", "outcome", "reward", "payload",
    "payload_b64", "hidden_hand", "hidden_hands", "hidden_opponent_hands",
    "remaining_hands",
}
_FORBIDDEN_OUTPUT_KEYS = {
    "authorization", "cookie", "cookies", "ca_private_key", "raw", "raw_data",
    "request_body", "response_body", "post_state", "outcome", "reward",
    "hidden_hand", "hidden_hands", "hidden_opponent_hands", "remaining_hands",
    "own_hand", "hand", "cards", "public_history", "history", "legal_rows",
    "model_input", "model_output",
}
_SAFE_STRING_MAX = 256


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _directory_snapshot(directory: Path) -> tuple[str, int]:
    # Capture directories may contain pre-existing game artifacts that are not
    # authorized inputs for this phase. Inspect metadata only; never open them.
    entries: list[tuple[str, str, int, int]] = []
    pending = [directory]
    while pending:
        parent = pending.pop()
        for path in sorted(parent.iterdir(), key=lambda item: item.name):
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise ValueError("LOCAL_CAPTURE_SELECTOR_INVALID")
            stat = path.stat()
            entries.append((path.relative_to(directory).as_posix(),
                            "directory" if path.is_dir() else "file",
                            0 if path.is_dir() else stat.st_size, stat.st_mtime_ns))
            if path.is_dir():
                pending.append(path)
    entries.sort()
    encoded = json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), len(entries)


def _line_count(path: Path) -> int:
    count = 0
    with path.open("rb") as stream:
        for _ in stream:
            count += 1
    return count


def _safe_error_code(value: object, default: str) -> str:
    code = value if isinstance(value, str) else default
    return code if code in _ALLOWED_UNSUPPORTED else default


def _walk_keys(value: object) -> Iterable[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def validate_safe_output(value: object) -> None:
    """Reject content-bearing evidence; only bounded aggregate metadata is allowed."""
    if _FORBIDDEN_OUTPUT_KEYS.intersection(key.lower() for key in _walk_keys(value)):
        raise ValueError("LOCAL_CAPTURE_PRIVACY_CONTRACT_INVALID")

    def visit(item: object) -> None:
        if item is None or type(item) in {bool, int, float}:
            return
        if isinstance(item, str):
            if len(item) > _SAFE_STRING_MAX or "\r" in item or "\n" in item:
                raise ValueError("LOCAL_CAPTURE_PRIVACY_CONTRACT_INVALID")
            if ":\\" in item or item.startswith(("/", "\\\\")):
                raise ValueError("LOCAL_CAPTURE_PRIVACY_CONTRACT_INVALID")
            return
        if isinstance(item, list):
            if len(item) > 256:
                raise ValueError("LOCAL_CAPTURE_PRIVACY_CONTRACT_INVALID")
            for child in item:
                visit(child)
            return
        if isinstance(item, dict):
            if len(item) > 64:
                raise ValueError("LOCAL_CAPTURE_PRIVACY_CONTRACT_INVALID")
            for key, child in item.items():
                if not isinstance(key, str) or len(key) > 64:
                    raise ValueError("LOCAL_CAPTURE_PRIVACY_CONTRACT_INVALID")
                visit(child)
            return
        raise ValueError("LOCAL_CAPTURE_PRIVACY_CONTRACT_INVALID")

    visit(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if len(encoded.encode("utf-8")) > 256 * 1024:
        raise ValueError("LOCAL_CAPTURE_PRIVACY_CONTRACT_INVALID")


def _validate_selector(entry: object) -> dict[str, object]:
    if not isinstance(entry, dict) or set(entry) != _ALLOWED_SELECTOR_KEYS:
        raise ValueError("LOCAL_CAPTURE_SELECTOR_INVALID")
    relative = Path(str(entry["relative_path"]))
    capture_id = entry["capture_id"]
    digest = entry["source_sha256"]
    if (not isinstance(capture_id, str) or len(capture_id) != 15
            or relative.is_absolute() or ".." in relative.parts
            or relative.parts != ("debug", "captures", capture_id, "frames.jsonl")
            or not isinstance(digest, str) or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
            or type(entry["expected_level"]) is not int
            or type(entry["expected_observer_seat"]) is not int
            or type(entry["expected_decision_count"]) is not int
            or not 1 <= entry["expected_level"] <= 13
            or not 0 <= entry["expected_observer_seat"] <= 3
            or entry["expected_decision_count"] <= 0
            or entry["status"] != "accepted_candidate"):
        raise ValueError("LOCAL_CAPTURE_SELECTOR_INVALID")
    return entry


def _closed_sidecar(path: Path, *, source_rows: int) -> tuple[bool, str | None]:
    sidecar = path.parent / "capture_status.json"
    if not sidecar.is_file():
        return False, "CAPTURE_CLOSED_SIDECAR_MISSING"
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("schema_version") != 1
                or data.get("state") != "closed" or data.get("frames") != source_rows
                or not isinstance(data.get("games"), list) or len(data["games"]) != 1
                or not isinstance(data.get("directions"), dict)
                or set(data["directions"]) != {"client", "server"}
                or not all(type(value) is int and value >= 0
                           for value in data["directions"].values())
                or sum(data["directions"].values()) != source_rows):
            return False, "CAPTURE_CLOSED_SIDECAR_INVALID"
    except (OSError, UnicodeError, ValueError, TypeError):
        return False, "CAPTURE_CLOSED_SIDECAR_INVALID"
    return True, None


def _open_runtime(model_manifest: Path, model_source: Path, checkpoint: Path) -> tuple[object | None, dict[str, object]]:
    from guance.infrastructure.model.errors import ModelBackendError
    from guance.infrastructure.model.factory import ModelFactory
    from guance.infrastructure.model.marvel_v2.deployment_bundle import CHECKPOINT_ENV, SOURCE_ENV

    summary: dict[str, object] = {
        "status": "unavailable", "provider": "guandan_marvel_v2", "runtime_version": "",
        "model_sha256": CHECKPOINT_SHA256, "bundle_fingerprint": "",
        "deployment_fingerprint": "", "production_ready": False, "strength_claim": False,
        "public_self_supported": False, "error_code": "MODEL_UNAVAILABLE",
    }
    if (model_manifest != EXPECTED_MODEL_MANIFEST.resolve()
            or model_source != EXPECTED_MODEL_SOURCE.resolve()
            or checkpoint != EXPECTED_CHECKPOINT.resolve()
            or not model_manifest.is_file() or not model_source.is_dir() or not checkpoint.is_file()
            or checkpoint.stat().st_size != CHECKPOINT_BYTES or _hash(checkpoint) != CHECKPOINT_SHA256):
        return None, summary
    old_source, old_checkpoint = os.environ.get(SOURCE_ENV), os.environ.get(CHECKPOINT_ENV)
    os.environ[SOURCE_ENV], os.environ[CHECKPOINT_ENV] = str(model_source), str(checkpoint)
    advisor = None
    try:
        advisor = ModelFactory().create(model_manifest)
        metadata = advisor.load()
        summary = {
            "status": metadata.status, "provider": metadata.provider,
            "runtime_version": metadata.runtime_version, "model_sha256": metadata.model_sha256,
            "bundle_fingerprint": metadata.bundle_fingerprint,
            "deployment_fingerprint": metadata.deployment_fingerprint,
            "production_ready": metadata.production_ready, "strength_claim": metadata.strength_claim,
            "public_self_supported": metadata.public_self_supported,
            "error_code": "GUANDAN_STATE_UNAVAILABLE" if metadata.status == "unavailable" else "",
        }
        if (metadata.provider != "guandan_marvel_v2" or metadata.model_sha256 != CHECKPOINT_SHA256
                or metadata.status != "unavailable" or metadata.production_ready
                or metadata.strength_claim or metadata.public_self_supported):
            advisor.close()
            return None, summary | {"status": "error", "error_code": "MODEL_VERSION_UNSUPPORTED"}
        return advisor, summary
    except ModelBackendError as exc:
        if advisor is not None:
            advisor.close()
        return None, summary | {"status": "error", "error_code": _safe_error_code(exc.code, "MODEL_UNAVAILABLE")}
    except Exception:
        if advisor is not None:
            advisor.close()
        return None, summary
    finally:
        if old_source is None:
            os.environ.pop(SOURCE_ENV, None)
        else:
            os.environ[SOURCE_ENV] = old_source
        if old_checkpoint is None:
            os.environ.pop(CHECKPOINT_ENV, None)
        else:
            os.environ[CHECKPOINT_ENV] = old_checkpoint


def _decision_summary(entry: dict[str, object], records: list[dict[str, Any]], advisor: object | None) -> dict[str, object]:
    from guance.contracts import CanonicalPublicGameView, GameObservation, ModelContractError
    from guance.infrastructure.model.errors import ModelBackendError

    unsupported = Counter()
    canonical_views = supported = recommendations = legal_actions = model_inferences = 0
    invalid_recommendations = 0
    coverage = Counter({"pass": 0, "single": 0, "pair": 0, "sequence": 0, "bomb": 0, "complex": 0})
    seats: set[int] = set()
    levels: set[int] = set()
    previous_state = previous_action = -1
    for record in records:
        try:
            if (record.get("status") != "accepted_candidate"
                    or record.get("privacy_scope") != "own_hand_and_public_actions_only"
                    or record.get("quality", {}).get("lost")
                    or record.get("quality", {}).get("status") in {"partial", "quarantine"}):
                raise ModelContractError("MODEL_INPUT_INVALID")
            pre_state = record["pre_state"]
            action = record["observed_action"]
            if not isinstance(pre_state, dict) or not isinstance(action, dict):
                raise ModelContractError("MODEL_INPUT_INVALID")
            if _FORBIDDEN_MODEL_KEYS.intersection(key.lower() for key in _walk_keys(pre_state)):
                raise ModelContractError("MODEL_INPUT_INVALID")
            state_version = int(pre_state["state_version"])
            action_index = int(action["index"])
            if state_version <= previous_state or action_index <= previous_action:
                raise ModelContractError("MODEL_INPUT_INVALID")
            previous_state, previous_action = state_version, action_index
            seat, level = int(pre_state["my_seat"]), int(pre_state["level"])
            if seat != entry["expected_observer_seat"] or level != entry["expected_level"]:
                raise ModelContractError("MODEL_INPUT_INVALID")
            seats.add(seat); levels.add(level)
            kind = action.get("kind")
            action_size = len(action.get("cards", ())) if isinstance(action.get("cards"), list) else -1
            if kind == "pass": coverage["pass"] += 1
            elif kind == "play" and action_size == 1: coverage["single"] += 1
            elif kind == "play" and action_size == 2: coverage["pair"] += 1
            elif kind == "play" and action_size > 2: coverage["complex"] += 1
            observation = GameObservation.from_dict(pre_state)
            view = CanonicalPublicGameView.from_observation(observation)
            view.to_dict()  # Contract validation only; never persisted.
            canonical_views += 1
            # V1 capture decisions lack exact MARVEL token/commit counters and
            # four-observer private-prefix history. Never synthesize them.
            unsupported["MARVEL_COUNTERS_UNAVAILABLE"] += 1
            if advisor is not None:
                try:
                    advisor.recommend(view, f"phase7-{entry['capture_id']}-{action_index}")
                except ModelBackendError as exc:
                    unsupported[_safe_error_code(exc.code, "GUANDAN_STATE_UNAVAILABLE")] += 1
                else:
                    # The current provider advertises no complete-action output
                    # contract. Any unexpected result is not acceptance evidence.
                    model_inferences += 1
                    recommendations += 1
                    invalid_recommendations += 1
                    unsupported["MODEL_OUTPUT_UNVERIFIED"] += 1
        except ModelContractError as exc:
            unsupported[_safe_error_code(exc.code, "MODEL_INPUT_INVALID")] += 1
        except (KeyError, TypeError, ValueError, OverflowError):
            unsupported["MODEL_INPUT_INVALID"] += 1
    evaluated = canonical_views
    return {
        "evaluated_decisions": evaluated, "supported_decisions": supported,
        "unsupported_decisions": evaluated - supported, "canonical_views": canonical_views,
        "model_inferences": model_inferences, "legal_actions": legal_actions,
        "recommendations": recommendations, "invalid_recommendations": invalid_recommendations,
        "unsupported_codes": dict(sorted(unsupported.items())),
        "coverage": {
            "seats": sorted(seats), "levels": sorted(levels),
            "pass": coverage["pass"], "single": coverage["single"], "pair": coverage["pair"],
            "sequence": coverage["sequence"], "bomb": coverage["bomb"],
            "complex": coverage["complex"],
        },
    }


def _capture(entry: dict[str, object], root: Path, advisor: object | None) -> dict[str, object]:
    from guance.infrastructure.training_data.legacy import read_legacy_episodes

    capture_id = str(entry["capture_id"])
    path = (root / str(entry["relative_path"])).resolve()
    expected = int(entry["expected_decision_count"])
    base = {
        "capture_id": capture_id, "source_sha256": str(entry["source_sha256"]),
        "expected_decisions": expected, "source_rows": 0, "evaluated_decisions": 0,
        "supported_decisions": 0, "unsupported_decisions": 0, "unevaluated_decisions": expected,
        "canonical_views": 0, "model_inferences": 0, "legal_actions": 0,
        "recommendations": 0, "invalid_recommendations": 0,
        "source_unchanged": False, "directory_unchanged": False,
        "settlement_verified": False, "capture_closed_verified": False,
        "status": "blocked", "unsupported_codes": {},
        "coverage": {"seats": [], "levels": [], "pass": 0, "single": 0, "pair": 0,
                     "sequence": 0, "bomb": 0, "complex": 0},
    }
    codes = Counter()
    try:
        if not path.is_file() or not path.is_relative_to(root):
            raise ValueError("LOCAL_CAPTURE_SELECTOR_INVALID")
        before_hash = _hash(path)
        before_dir, _ = _directory_snapshot(path.parent)
        if before_hash != entry["source_sha256"]:
            raise ValueError("LOCAL_CAPTURE_SELECTOR_INVALID")
        source_rows = _line_count(path)
        closed, closed_code = _closed_sidecar(path, source_rows=source_rows)
        if closed_code:
            codes[closed_code] += 1
        episodes, report = read_legacy_episodes(path)
        after_hash = _hash(path)
        after_dir, _ = _directory_snapshot(path.parent)
        base.update(source_rows=source_rows, source_unchanged=before_hash == after_hash,
                    directory_unchanged=before_dir == after_dir,
                    capture_closed_verified=closed)
        if before_hash != after_hash or before_dir != after_dir:
            codes["LOCAL_CAPTURE_SELECTOR_INVALID"] += 1
        if report.get("status") != "converted" or report.get("errors"):
            codes["LOCAL_CAPTURE_CONVERSION_REJECTED"] += 1
            if any(isinstance(error, dict) and error.get("code") == "protocol_rejected"
                   for error in report.get("errors", [])):
                codes["CAPTURE_PROTOCOL_REJECTED"] += 1
        records = [record for episode in episodes for record in episode.get("records", [])]
        eligible = (report.get("status") == "converted" and not report.get("errors")
                    and report.get("accepted_candidates") == expected
                    and report.get("partial") == 0 and report.get("quarantined") == 0
                    and len(episodes) == 1 and len(records) == expected
                    and episodes[0].get("status") == "accepted_candidate"
                    and not episodes[0].get("lost"))
        if report.get("accepted_candidates") != expected or len(records) != expected:
            codes["LOCAL_CAPTURE_DECISION_COUNT_MISMATCH"] += 1
        if not eligible:
            codes["LOCAL_CAPTURE_DECISIONS_UNAVAILABLE"] += expected
        else:
            if advisor is None:
                codes["MODEL_UNAVAILABLE"] += expected
            decision = _decision_summary(entry, records, advisor)
            base.update(decision)
            base["unevaluated_decisions"] = expected - int(decision["evaluated_decisions"])
            for code, count in decision["unsupported_codes"].items():
                codes[code] += count
            settlement = any(bool(record.get("post_state", {}).get("finish_ranks_by_seat"))
                             for record in records)
            base["settlement_verified"] = settlement
            if not settlement:
                codes["LOCAL_CAPTURE_SETTLEMENT_UNVERIFIED"] += 1
        decision_complete = (base["evaluated_decisions"] == expected
                             and base["unevaluated_decisions"] == 0)
        if (decision_complete and advisor is not None and base["invalid_recommendations"] == 0
                and base["settlement_verified"] and closed
                and base["source_unchanged"] and base["directory_unchanged"]):
            base["status"] = "passed"
    except ValueError as exc:
        codes[_safe_error_code(str(exc), "LOCAL_CAPTURE_SELECTOR_INVALID")] += 1
    except Exception:
        codes["LOCAL_CAPTURE_CONVERSION_REJECTED"] += 1
    base["unsupported_codes"] = dict(sorted(codes.items()))
    return base


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--model-manifest", type=Path, required=True)
    parser.add_argument("--model-source", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(CODE_ROOT / "src"))
    try:
        if root != CODE_ROOT.resolve():
            raise ValueError("LOCAL_CAPTURE_SELECTOR_INVALID")
        entries = json.loads(MANIFEST.read_text(encoding="utf-8"))
        if not isinstance(entries, list) or len(entries) != 5:
            raise ValueError("LOCAL_CAPTURE_SELECTOR_INVALID")
        selectors = [_validate_selector(entry) for entry in entries]
        advisor, runtime = _open_runtime(args.model_manifest.resolve(), args.model_source.resolve(),
                                         args.checkpoint.resolve())
        captures = [_capture(entry, root, advisor) for entry in selectors]
        if advisor is not None:
            advisor.close()
        coverage = {
            "seats": sorted({seat for item in captures for seat in item["coverage"]["seats"]}),
            "levels": sorted({level for item in captures for level in item["coverage"]["levels"]}),
            **{name: sum(int(item["coverage"][name]) for item in captures)
               for name in ("pass", "single", "pair", "sequence", "bomb", "complex")},
        }
        totals = {
            "expected_decisions": sum(int(item["expected_decisions"]) for item in captures),
            "evaluated_decisions": sum(int(item["evaluated_decisions"]) for item in captures),
            "supported_decisions": sum(int(item["supported_decisions"]) for item in captures),
            "unsupported_decisions": sum(int(item["unsupported_decisions"]) for item in captures),
            "unevaluated_decisions": sum(int(item["unevaluated_decisions"]) for item in captures),
            "captures_passed": sum(item["status"] == "passed" for item in captures),
            "captures_blocked": sum(item["status"] != "passed" for item in captures),
            "coverage": coverage,
        }
        coverage_complete = (set(coverage["seats"]) == {0, 1, 2, 3}
                             and set(coverage["levels"]) == {4, 6, 7, 8}
                             and all(int(coverage[name]) > 0 for name in
                                     ("pass", "single", "pair", "sequence", "bomb", "complex")))
        runtime_exact = (runtime["status"] == "unavailable"
                         and runtime["error_code"] == "GUANDAN_STATE_UNAVAILABLE"
                         and runtime["model_sha256"] == CHECKPOINT_SHA256
                         and bool(runtime["bundle_fingerprint"])
                         and bool(runtime["deployment_fingerprint"])
                         and runtime["production_ready"] is False
                         and runtime["strength_claim"] is False
                         and runtime["public_self_supported"] is False)
        status = ("passed" if totals["captures_passed"] == 5 and coverage_complete
                  and runtime_exact else "blocked")
        result = {"schema": SCHEMA, "task_id": TASK_ID, "status": status,
                  "runtime": runtime, "captures": captures, "totals": totals}
    except Exception:
        result = {
            "schema": SCHEMA, "task_id": TASK_ID, "status": "blocked",
            "runtime": {"status": "error", "provider": "guandan_marvel_v2",
                        "runtime_version": "", "model_sha256": CHECKPOINT_SHA256,
                        "bundle_fingerprint": "", "deployment_fingerprint": "",
                        "production_ready": False, "strength_claim": False,
                        "public_self_supported": False, "error_code": "LOCAL_CAPTURE_SELECTOR_INVALID"},
            "captures": [],
            "totals": {"expected_decisions": 0, "evaluated_decisions": 0,
                       "supported_decisions": 0, "unsupported_decisions": 0,
                       "unevaluated_decisions": 0, "captures_passed": 0,
                       "captures_blocked": 5,
                       "coverage": {"seats": [], "levels": [], "pass": 0,
                                    "single": 0, "pair": 0, "sequence": 0,
                                    "bomb": 0, "complex": 0}},
        }
    validate_safe_output(result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
