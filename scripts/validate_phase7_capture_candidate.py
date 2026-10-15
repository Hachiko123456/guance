"""Read-only validator for future CanonicalLocalCaptureAcceptanceInput.v1 candidates."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tests" / "local_capture_manifest.json"
OUTPUT_SCHEMA = "guance-phase7-capture-candidate-validation-v1"
INPUT_SCHEMA = "CanonicalLocalCaptureAcceptanceInput.v1"
SIDECAR_SCHEMA = "guance-capture-closed-sidecar-v1"
CAPTURE_SCHEMA = "guance-debug-frame-jsonl-v1"
PROTOCOL_IDENTITY = "TencentDaguandanObservedProtocol.v20261014"
TARGET_HOST = "hlxyxws.huanle.qq.com"
CANONICAL_PRODUCER = "guance.CanonicalCaptureConverter"
CANONICAL_PRODUCER_VERSION = "1"
COUNTER_CONTRACT = {
    "frame": "source_sequence", "state": "canonical_state_version",
    "action": "public_action_index", "token": "marvel_token_step",
    "commit": "marvel_committed_step",
}
REQUIRED_CAPABILITIES = frozenset({
    "public_self_canonical", "exact_four_observer_history", "private_prefix",
    "declaration", "tribute_return", "token_step", "committed_step",
    "legal_action_counters",
})
REQUIRED_QUALITY = frozenset({"no_loss", "no_gap", "no_partial", "no_quarantine"})


def _hash(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256(); size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk); digest.update(chunk)
    return digest.hexdigest(), size


def _metadata(path: Path) -> dict[str, Any]:
    rows = 0; directions: Counter[str] = Counter(); hosts: Counter[str] = Counter()
    epochs: list[dict[str, Any]] = []; current_connection = ""; last_sequence: int | None = None
    with path.open("rb") as stream:
        for line in stream:
            rows += 1
            record = json.loads(line.decode("utf-8-sig"))
            if not isinstance(record, dict):
                raise ValueError("FRAME_METADATA_INVALID")
            direction = record.get("direction"); connection = record.get("connection_id")
            sequence = record.get("sequence"); host = record.get("host")
            if (record.get("schema_version") != 1 or record.get("dropped") or record.get("injected")
                    or direction not in {"client", "server"} or type(connection) is not str
                    or not connection or type(sequence) is not int or not 0 <= sequence < 2**63
                    or type(host) is not str or not host):
                raise ValueError("FRAME_METADATA_INVALID")
            directions[direction] += 1; hosts[host] += 1
            if not epochs or connection != current_connection:
                current_connection = connection
                epochs.append({"ordinal": len(epochs) + 1, "start_sequence": sequence,
                               "end_sequence": sequence, "frames": 0,
                               "directions": Counter(), "complete": True})
            epoch = epochs[-1]
            if epoch["frames"] and sequence != epoch["end_sequence"] + 1:
                epoch["complete"] = False
            epoch["end_sequence"] = sequence; epoch["frames"] += 1
            epoch["directions"][direction] += 1; last_sequence = sequence
    public_epochs = [{"ordinal": item["ordinal"], "start_sequence": item["start_sequence"],
                      "end_sequence": item["end_sequence"], "frames": item["frames"],
                      "directions": dict(sorted(item["directions"].items()))}
                     for item in epochs]
    return {"rows": rows, "directions": dict(sorted(directions.items())),
            "hosts": dict(sorted(hosts.items())), "connection_count": len(epochs),
            "connection_epochs": public_epochs,
            "sequences_complete": all(item["complete"] for item in epochs),
            "final_sequence": last_sequence}


def _sidecar(path: Path, capture_id: str, source_hash: str, source_bytes: int,
             metadata: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    sidecar_path = path.parent / "capture_status.json"
    if not sidecar_path.is_file():
        return "CAPTURE_CLOSED_SIDECAR_MISSING", None
    try:
        data = json.loads(sidecar_path.read_text(encoding="utf-8"))
        expected_keys = {
            "schema", "schema_version", "capture_schema", "producer", "producer_version",
            "protocol_identity", "capture_id", "source_filename", "source_sha256",
            "source_bytes", "state", "started_at", "closed_at", "close_reason", "frames",
            "bytes", "directions", "hosts", "connection_epochs", "final_sequence",
            "current_game_id", "games",
        }
        valid = (isinstance(data, dict) and set(data) == expected_keys
            and data.get("schema") == SIDECAR_SCHEMA and data.get("schema_version") == 1
            and data.get("capture_schema") == CAPTURE_SCHEMA
            and data.get("producer") == "guance.DebugFrameRecorder"
            and data.get("producer_version") == "1"
            and data.get("protocol_identity") == PROTOCOL_IDENTITY
            and data.get("capture_id") == capture_id and data.get("source_filename") == "frames.jsonl"
            and data.get("source_sha256") == source_hash and data.get("source_bytes") == source_bytes
            and data.get("frames") == metadata["rows"] and data.get("bytes") == source_bytes
            and data.get("directions") == metadata["directions"]
            and data.get("hosts") == metadata["hosts"]
            and data.get("connection_epochs") == metadata["connection_epochs"]
            and data.get("final_sequence") == metadata["final_sequence"]
            and data.get("state") == "closed" and isinstance(data.get("closed_at"), str)
            and bool(data.get("closed_at")) and isinstance(data.get("close_reason"), str)
            and bool(data.get("close_reason")) and data.get("current_game_id") == ""
            and isinstance(data.get("games"), list) and bool(data["games"])
            and all(isinstance(game, dict)
                    and set(game) == {"game_id", "status", "start_frame_index", "end_frame_index",
                                      "start_sequence", "end_sequence"}
                    and game.get("status") == "settled" for game in data["games"])
            and metadata["sequences_complete"])
        return ("CAPTURE_CLOSED_SIDECAR_VALID", data) if valid else ("CAPTURE_CLOSED_SIDECAR_INVALID", None)
    except (OSError, UnicodeError, ValueError, TypeError):
        return "CAPTURE_CLOSED_SIDECAR_INVALID", None


def _acceptance_input(path: Path, capture_id: str, source_hash: str, source_bytes: int,
                      metadata: dict[str, Any], sidecar_hash: str) -> tuple[list[str], dict[str, Any] | None]:
    contract_path = path.parent / "phase7_acceptance_input.json"
    if not contract_path.is_file():
        return ["CANONICAL_INPUT_MANIFEST_MISSING"], None
    try:
        data = json.loads(contract_path.read_text(encoding="utf-8"))
        codes: list[str] = []
        expected_keys = {
            "schema", "schema_version", "producer", "producer_version", "capture_id",
            "capture_schema", "protocol_identity", "target_host_identity", "source_filename",
            "source_sha256", "source_bytes", "source_rows", "direction_counts",
            "connection_epochs", "game_count", "decision_count", "level", "observer_seat",
            "observer_evidence", "counter_contract", "capabilities", "quality",
            "closed_sidecar_sha256",
        }
        if (not isinstance(data, dict) or set(data) != expected_keys
                or data.get("schema") != INPUT_SCHEMA or data.get("schema_version") != 1):
            codes.append("CANONICAL_INPUT_SCHEMA_INVALID")
        if (data.get("producer") != CANONICAL_PRODUCER
                or data.get("producer_version") != CANONICAL_PRODUCER_VERSION):
            codes.append("CANONICAL_PRODUCER_UNSUPPORTED")
        if data.get("capture_id") != capture_id: codes.append("CAPTURE_ID_MISMATCH")
        if data.get("capture_schema") != CAPTURE_SCHEMA: codes.append("CAPTURE_SCHEMA_UNSUPPORTED")
        if data.get("protocol_identity") != PROTOCOL_IDENTITY: codes.append("PROTOCOL_IDENTITY_UNSUPPORTED")
        if data.get("target_host_identity") != TARGET_HOST: codes.append("TARGET_HOST_IDENTITY_MISMATCH")
        if (data.get("source_filename") != "frames.jsonl" or data.get("source_sha256") != source_hash
                or data.get("source_bytes") != source_bytes or data.get("source_rows") != metadata["rows"]):
            codes.append("SOURCE_IDENTITY_MISMATCH")
        if data.get("direction_counts") != metadata["directions"]: codes.append("DIRECTION_COUNTS_MISMATCH")
        if data.get("connection_epochs") != metadata["connection_epochs"]: codes.append("CONNECTION_EPOCHS_MISMATCH")
        if data.get("game_count") != 1: codes.append("GAME_COUNT_INVALID")
        if data.get("counter_contract") != COUNTER_CONTRACT: codes.append("COUNTER_CONTRACT_INVALID")
        if data.get("closed_sidecar_sha256") != sidecar_hash: codes.append("SIDECAR_IDENTITY_MISMATCH")
        if type(data.get("decision_count")) is not int or data.get("decision_count") <= 0:
            codes.append("DECISION_COUNT_INVALID")
        if type(data.get("level")) is not int or not 1 <= data.get("level") <= 13:
            codes.append("LEVEL_INVALID")
        if type(data.get("observer_seat")) is not int or data.get("observer_seat") not in range(4):
            codes.append("OBSERVER_IDENTITY_UNAVAILABLE")
        if not isinstance(data.get("observer_evidence"), str) or not data.get("observer_evidence"):
            codes.append("OBSERVER_EVIDENCE_MISSING")
        capabilities = data.get("capabilities")
        if (not isinstance(capabilities, dict) or set(capabilities) != REQUIRED_CAPABILITIES
                or not all(value is True for value in capabilities.values())):
            codes.append("CAPABILITIES_INCOMPLETE")
        quality = data.get("quality")
        if (not isinstance(quality, dict) or set(quality) != REQUIRED_QUALITY
                or not all(value is True for value in quality.values())):
            codes.append("QUALITY_INCOMPLETE")
        return sorted(set(codes)), data if not codes else None
    except (OSError, UnicodeError, ValueError, TypeError):
        return ["CANONICAL_INPUT_MANIFEST_INVALID"], None


def validate_candidate(entry: dict[str, object], root: Path) -> dict[str, object]:
    path = (root / str(entry["relative_path"])).resolve()
    if not path.is_file() or not path.is_relative_to(root):
        raise ValueError("unsafe_selector")
    before_hash, source_bytes = _hash(path)
    capture_id = str(entry["capture_id"])
    if before_hash != entry["source_sha256"]:
        return {"capture_id": capture_id, "schema": "unknown", "producer": "",
                "producer_version": "", "protocol_identity": "",
                "source_sha256": before_hash, "source_bytes": source_bytes,
                "source_rows": 0, "sidecar_status": "blocked",
                "canonical_journal_status": "blocked", "game_count": 0,
                "decision_count": 0, "levels": [], "observer_seats": [],
                "coverage": {}, "capability_states": {}, "quality_states": {},
                "missing_fact_codes": ["SOURCE_HASH_MISMATCH"], "eligible": False}
    metadata = _metadata(path)
    manifest_path = path.parent / "phase7_acceptance_input.json"
    if manifest_path.is_file():
        try:
            from guance.infrastructure.capture.canonical_capture import (
                CanonicalCaptureError, MANIFEST_SCHEMA, strict_json_loads,
                validate_v2_candidate,
            )
            try:
                manifest = strict_json_loads(manifest_path.read_text(encoding="utf-8"))
            except CanonicalCaptureError as exc:
                return {"capture_id": capture_id, "schema": "unknown", "producer": "",
                        "producer_version": "", "protocol_identity": PROTOCOL_IDENTITY,
                        "source_sha256": before_hash, "source_bytes": source_bytes,
                        "source_rows": metadata["rows"], "sidecar_status": "blocked",
                        "canonical_journal_status": "blocked", "game_count": 0,
                        "decision_count": 0, "levels": [], "observer_seats": [],
                        "coverage": {}, "capability_states": {}, "quality_states": {},
                        "missing_fact_codes": [exc.code], "eligible": False}
            if isinstance(manifest, dict) and manifest.get("schema") == MANIFEST_SCHEMA:
                try:
                    return validate_v2_candidate(path.parent, capture_id)
                except CanonicalCaptureError as exc:
                    return {"capture_id": capture_id, "schema": MANIFEST_SCHEMA,
                            "producer": "guance.CanonicalCaptureConverter", "producer_version": "2",
                            "protocol_identity": PROTOCOL_IDENTITY, "source_sha256": before_hash,
                            "source_bytes": source_bytes, "source_rows": metadata["rows"],
                            "sidecar_status": "blocked", "canonical_journal_status": "blocked",
                            "game_count": 0, "decision_count": 0, "levels": [],
                            "observer_seats": [], "coverage": {}, "capability_states": {},
                            "quality_states": {}, "missing_fact_codes": [exc.code],
                            "eligible": False}
        except (OSError, UnicodeError, ValueError, TypeError):
            pass
    sidecar_status, sidecar = _sidecar(path, capture_id, before_hash,
                                       source_bytes, metadata)
    codes: list[str] = []
    if sidecar_status != "CAPTURE_CLOSED_SIDECAR_VALID":
        codes.append(sidecar_status)
        sidecar_hash = ""
    else:
        sidecar_hash, _ = _hash(path.parent / "capture_status.json")
    input_codes, acceptance = _acceptance_input(path, str(entry["capture_id"]), before_hash,
                                                 source_bytes, metadata, sidecar_hash)
    codes.extend(input_codes)
    if acceptance is not None:
        codes.append("V1_NOT_POSITIVE_ELIGIBLE")
    after_hash, after_bytes = _hash(path)
    if (after_hash, after_bytes) != (before_hash, source_bytes):
        codes.append("SOURCE_CHANGED_DURING_VALIDATION")
    protocol = acceptance.get("protocol_identity", "") if acceptance else (
        sidecar.get("protocol_identity", "") if sidecar else "")
    producer = ((sidecar.get("producer", "") + "+" + acceptance.get("producer", ""))
                if sidecar and acceptance else (sidecar.get("producer", "") if sidecar else ""))
    return {"capture_id": entry["capture_id"], "source_sha256": before_hash,
            "source_bytes": source_bytes, "source_rows": metadata["rows"],
            "sidecar_status": sidecar_status,
            "canonical_journal_status": "not_available_v1",
            "protocol_identity": protocol,
            "producer_identity": producer, "direction_counts": metadata["directions"],
            "connection_count": metadata["connection_count"],
            "game_count": len(sidecar.get("games", [])) if sidecar else 0,
            "decision_count": acceptance.get("decision_count", 0) if acceptance else 0,
            "missing_fact_codes": sorted(set(codes)), "eligible": not codes}


def _safe(value: object) -> None:
    forbidden = {"payload", "payload_b64", "cards", "hand", "state", "action", "history",
                 "cookie", "authorization", "hidden_opponent_hands"}
    def walk(item: object) -> None:
        if item is None or type(item) in {bool, int, float}: return
        if isinstance(item, str):
            if len(item) > 256 or "\\" in item or "/" in item or "\n" in item or "\r" in item:
                raise ValueError("unsafe_output")
            return
        if isinstance(item, list):
            for child in item: walk(child)
            return
        if isinstance(item, dict):
            if forbidden.intersection(str(key).lower() for key in item): raise ValueError("unsafe_output")
            for child in item.values(): walk(child)
            return
        raise ValueError("unsafe_output")
    walk(value)


def main() -> int:
    entries = json.loads(MANIFEST.read_text(encoding="utf-8"))
    ids = [entry["capture_id"] for entry in entries]
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--capture-id", action="append", choices=ids)
    args = parser.parse_args(); root = args.root.resolve(); selected = set(args.capture_id or ids)
    try:
        if root != ROOT.resolve(): raise ValueError("unsafe_root")
        captures = [validate_candidate(entry, root) for entry in entries if entry["capture_id"] in selected]
        value = {"schema": OUTPUT_SCHEMA, "status": "eligible" if captures and all(x["eligible"] for x in captures) else "blocked",
                 "captures": captures}
        _safe(value); print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0 if value["status"] == "eligible" else 2
    except Exception:
        print(json.dumps({"schema": OUTPUT_SCHEMA, "status": "blocked", "captures": [],
                          "missing_fact_codes": ["INTERNAL_VALIDATION_REJECTED"]}, sort_keys=True,
                         separators=(",", ":")))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
