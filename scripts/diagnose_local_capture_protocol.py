"""Bounded read-only protocol rejection diagnostics for fixed Phase 7 captures.

Server payloads are decoded only in memory. Client payload fields are never read
or decoded. Output contains structural ordinals, integer field identifiers, and
allow-listed error codes only.
"""
from __future__ import annotations

import argparse
import base64
import binascii
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tests" / "local_capture_manifest.json"
SCHEMA = "guance-gmv2-phase7-protocol-rejection-diagnostic-v1"
GAME_HOST = "hlxyxws.huanle.qq.com"
MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_PAYLOAD_BYTES = 1024 * 1024
CODES = frozenset({
    "FRAME_METADATA_INVALID", "FRAME_SEQUENCE_REPEATED", "FRAME_SEQUENCE_GAP",
    "FRAME_CONNECTION_SWITCH_UNSAFE", "AGGREGATE_ENVELOPE_INVALID",
    "AGGREGATE_MESSAGE_TRUNCATED", "COMMAND_UNKNOWN", "COMMAND_REQUIRED_FIELD_MISSING",
    "COMMAND_REQUIRED_FIELD_TYPE_INVALID", "COMMAND_VALUE_OUT_OF_RANGE",
    "COMMAND_SEMANTICS_UNSUPPORTED", "DECLARATION_FACTS_INCOMPLETE",
    "EXCHANGE_FACTS_INCOMPLETE", "OBSERVER_IDENTITY_UNAVAILABLE",
    "TOKEN_STEP_UNAVAILABLE", "COMMITTED_STEP_UNAVAILABLE",
    "FOUR_OBSERVER_HISTORY_UNAVAILABLE", "PROTOCOL_VERSION_UNSUPPORTED",
    "CAPTURE_INCOMPLETE", "CAPTURE_CLOSED_SIDECAR_MISSING",
    "CAPTURE_CLOSED_SIDECAR_INVALID", "INTERNAL_DIAGNOSTIC_REJECTED",
})
STAGES = frozenset({"frame_metadata", "transport_sequence", "aggregate_envelope",
                    "aggregate_message", "command_contract", "capture_complete"})
SCALAR_FIELDS = {
    3001: (1, 4), 3004: (1,), 3005: (2, 3, 4, 5, 6),
    3006: (2, 3, 4), 3019: (2,),
}


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256(); size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk); digest.update(chunk)
    return digest.hexdigest(), size


def _directory_metadata(directory: Path) -> str:
    entries: list[tuple[str, str, int, int]] = []
    pending = [directory]
    while pending:
        parent = pending.pop()
        for path in sorted(parent.iterdir(), key=lambda item: item.name):
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise ValueError("unsafe_capture_entry")
            stat = path.stat()
            entries.append((path.relative_to(directory).as_posix(),
                            "directory" if path.is_dir() else "file",
                            0 if path.is_dir() else stat.st_size, stat.st_mtime_ns))
            if path.is_dir():
                pending.append(path)
    entries.sort()
    return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()



def _capture_metadata(path: Path) -> tuple[int, Counter[str], int]:
    """Count complete capture metadata without accessing any payload field."""
    rows = 0
    directions: Counter[str] = Counter()
    connections: set[str] = set()
    with path.open("rb") as stream:
        for line in stream:
            rows += 1
            try:
                record = json.loads(line.decode("utf-8-sig"))
            except (UnicodeError, ValueError):
                continue
            if not isinstance(record, dict):
                continue
            direction = record.get("direction")
            directions[str(direction) if isinstance(direction, str) else "unknown"] += 1
            if record.get("host") == GAME_HOST and isinstance(record.get("connection_id"), str):
                connections.add(record["connection_id"])
    return rows, directions, len(connections)

def _failure(*, frame_ordinal: int, source_sequence: int, connection_ordinal: int,
             direction: str, aggregate_message_ordinal: int, command_id: int,
             parser_stage: str, bounded_error_code: str,
             required_field_ids: tuple[int, ...] = (),
             missing_fact_codes: tuple[str, ...] = ()) -> dict[str, object]:
    if parser_stage not in STAGES or bounded_error_code not in CODES:
        bounded_error_code = "INTERNAL_DIAGNOSTIC_REJECTED"
        parser_stage = "command_contract"
        required_field_ids = ()
        missing_fact_codes = ()
    return {
        "frame_ordinal": int(frame_ordinal),
        "source_sequence": int(source_sequence),
        "connection_ordinal": int(connection_ordinal),
        "direction": direction if direction in {"server", "client", "unknown"} else "unknown",
        "aggregate_message_ordinal": int(aggregate_message_ordinal),
        "command_id": int(command_id),
        "parser_stage": parser_stage,
        "bounded_error_code": bounded_error_code,
        "required_field_ids": list(required_field_ids),
        "missing_fact_codes": list(missing_fact_codes),
    }


def _scalar_problem(group: dict[int, list[tuple[int, int | bytes]]],
                    fields: tuple[int, ...]) -> tuple[str | None, tuple[int, ...]]:
    missing = tuple(number for number in fields if number not in group)
    if missing:
        return "COMMAND_REQUIRED_FIELD_MISSING", missing
    wrong = tuple(number for number in fields
                  if len(group.get(number, ())) != 1 or group[number][0][0] != 0
                  or type(group[number][0][1]) is not int)
    if wrong:
        return "COMMAND_REQUIRED_FIELD_TYPE_INVALID", wrong
    return None, ()


def _packed_problem(group: dict[int, list[tuple[int, int | bytes]]], number: int,
                    *, required: bool) -> tuple[str | None, tuple[int, ...]]:
    if number not in group:
        return ("COMMAND_REQUIRED_FIELD_MISSING", (number,)) if required else (None, ())
    values = group[number]
    if len(values) != 1 or values[0][0] != 2 or not isinstance(values[0][1], bytes):
        return "COMMAND_REQUIRED_FIELD_TYPE_INVALID", (number,)
    return None, ()


def _scalar_value(group: dict[int, list[tuple[int, int | bytes]]], number: int) -> int:
    value = group[number][0][1]
    assert type(value) is int
    return value


def _has_3019_references(group: dict[int, list[tuple[int, int | bytes]]], wire: Any) -> bool:
    if 3 not in group:
        return False
    try:
        values = group[3]
        if len(values) != 1 or values[0][0] != 2 or not isinstance(values[0][1], bytes):
            return False
        nested = wire.fields(values[0][1])
        for entries in nested.values():
            for wire_type, blob in entries:
                if wire_type != 2 or not isinstance(blob, bytes):
                    continue
                item = wire.fields(blob)
                if 2 in item and wire.packed_field(item, 2):
                    return True
    except (ValueError, TypeError):
        return False
    return False


def _command_problem(command: int, group: dict[int, list[tuple[int, int | bytes]]],
                     wire: Any) -> tuple[str | None, tuple[int, ...], tuple[str, ...]]:
    if command not in {14, 112, 3000, 3001, 3002, 3003, 3004, 3005, 3006,
                       3007, 3008, 3009, 3010, 3011, 3019}:
        return "COMMAND_UNKNOWN", (), ()
    scalar_fields = SCALAR_FIELDS.get(command, ())
    if command == 3019 and not _has_3019_references(group, wire):
        scalar_fields = ()
    code, fields = _scalar_problem(group, scalar_fields)
    if code:
        facts = {
            3005: ("NEXT_ACTOR_UNAVAILABLE",),
            3006: ("NEXT_ACTOR_UNAVAILABLE", "PASS_RESET_FACT_UNAVAILABLE"),
            3019: ("OBSERVER_IDENTITY_UNAVAILABLE",),
        }.get(command, ())
        return code, fields, facts
    if command == 3001:
        code, fields = _packed_problem(group, 2, required=True)
        if code:
            return code, fields, ("INITIAL_HAND_FACTS_INCOMPLETE",)
        try:
            hand = wire.packed_field(group, 2)
        except (ValueError, TypeError):
            return "COMMAND_REQUIRED_FIELD_TYPE_INVALID", (2,), ("INITIAL_HAND_FACTS_INCOMPLETE",)
        if _scalar_value(group, 1) != 27 or len(hand) != 27 or not 1 <= _scalar_value(group, 4) <= 13:
            return "COMMAND_VALUE_OUT_OF_RANGE", (1, 2, 4), ()
    elif command == 3004:
        if _scalar_value(group, 1) not in range(4):
            return "COMMAND_VALUE_OUT_OF_RANGE", (1,), ()
    elif command == 3019 and scalar_fields:
        if _scalar_value(group, 2) not in range(4):
            return "COMMAND_VALUE_OUT_OF_RANGE", (2,), ("OBSERVER_IDENTITY_UNAVAILABLE",)
    elif command == 3005:
        code, fields = _packed_problem(group, 7, required=True)
        if code:
            return code, fields, ("DECLARATION_FACTS_INCOMPLETE",)
        try:
            cards = wire.packed_field(group, 7)
        except (ValueError, TypeError):
            return "COMMAND_REQUIRED_FIELD_TYPE_INVALID", (7,), ("DECLARATION_FACTS_INCOMPLETE",)
        actor, next_actor = _scalar_value(group, 2), _scalar_value(group, 3)
        if actor not in range(4) or (next_actor not in range(4) and next_actor != (1 << 64) - 1):
            return "COMMAND_VALUE_OUT_OF_RANGE", (2, 3), ()
        if (not cards or len(cards) != _scalar_value(group, 6)
                or not 0 <= _scalar_value(group, 5) <= 27 or _scalar_value(group, 4) <= 0):
            return "COMMAND_VALUE_OUT_OF_RANGE", (4, 5, 6, 7), ()
    elif command == 3006:
        actor, next_actor, reset = (_scalar_value(group, number) for number in (2, 3, 4))
        if (actor not in range(4)
                or (next_actor not in range(4) and next_actor != (1 << 64) - 1)
                or reset not in (0, 1, 2)):
            return "COMMAND_VALUE_OUT_OF_RANGE", (2, 3, 4), ()
    elif command == 3008:
        code, fields = _packed_problem(group, 3, required=True)
        if code:
            return code, fields, ("TERMINAL_BOUNDARY_UNAVAILABLE",)
        try:
            ranks = wire.packed_field(group, 3)
        except (ValueError, TypeError):
            return "COMMAND_REQUIRED_FIELD_TYPE_INVALID", (3,), ("TERMINAL_BOUNDARY_UNAVAILABLE",)
        if len(ranks) != 4 or sorted(ranks) != [1, 2, 3, 4]:
            return "COMMAND_VALUE_OUT_OF_RANGE", (3,), ("TERMINAL_BOUNDARY_UNAVAILABLE",)
    return None, (), ()


def _sidecar_status(path: Path, rows: int, directions: Counter[str]) -> str:
    sidecar = path.parent / "capture_status.json"
    if not sidecar.is_file():
        return "CAPTURE_CLOSED_SIDECAR_MISSING"
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        valid = (isinstance(data, dict) and data.get("schema_version") == 1
                 and data.get("state") == "closed" and data.get("frames") == rows
                 and isinstance(data.get("directions"), dict)
                 and data["directions"] == dict(sorted(directions.items()))
                 and isinstance(data.get("games"), list) and len(data["games"]) == 1)
        return "LEGACY_CLOSED_SIDECAR_VERIFIED" if valid else "CAPTURE_CLOSED_SIDECAR_INVALID"
    except (OSError, UnicodeError, ValueError, TypeError):
        return "CAPTURE_CLOSED_SIDECAR_INVALID"


def diagnose_capture(entry: dict[str, object], root: Path) -> dict[str, object]:
    sys.path.insert(0, str(ROOT / "src"))
    from guance.infrastructure.protocol import live, wire

    path = (root / str(entry["relative_path"])).resolve()
    if not path.is_file() or not path.is_relative_to(root):
        raise ValueError("unsafe_selector")
    before_hash, source_bytes = _sha256(path)
    before_directory = _directory_metadata(path.parent)
    if before_hash != entry["source_sha256"]:
        raise ValueError("source_hash_mismatch")
    rows = 0
    directions: Counter[str] = Counter()
    connections: dict[str, int] = {}
    last_sequence: dict[str, int] = {}
    first_failure: dict[str, object] | None = None
    with path.open("rb") as stream:
        while True:
            line = stream.readline(MAX_LINE_BYTES + 1)
            if not line:
                break
            rows += 1
            if len(line) > MAX_LINE_BYTES:
                first_failure = _failure(frame_ordinal=rows, source_sequence=-1,
                    connection_ordinal=0, direction="unknown", aggregate_message_ordinal=0,
                    command_id=0, parser_stage="frame_metadata",
                    bounded_error_code="FRAME_METADATA_INVALID")
                break
            try:
                record = json.loads(line.decode("utf-8-sig"))
            except (UnicodeError, ValueError):
                first_failure = _failure(frame_ordinal=rows, source_sequence=-1,
                    connection_ordinal=0, direction="unknown", aggregate_message_ordinal=0,
                    command_id=0, parser_stage="frame_metadata",
                    bounded_error_code="FRAME_METADATA_INVALID")
                break
            if not isinstance(record, dict):
                first_failure = _failure(frame_ordinal=rows, source_sequence=-1,
                    connection_ordinal=0, direction="unknown", aggregate_message_ordinal=0,
                    command_id=0, parser_stage="frame_metadata",
                    bounded_error_code="FRAME_METADATA_INVALID")
                break
            direction = record.get("direction")
            directions[str(direction) if isinstance(direction, str) else "unknown"] += 1
            if record.get("host") != GAME_HOST:
                continue
            connection = record.get("connection_id")
            sequence = record.get("sequence")
            if (direction not in {"client", "server"} or type(connection) is not str
                    or not connection or len(connection) > 4096 or type(sequence) is not int
                    or not 0 <= sequence < 2**63 or type(record.get("schema_version")) is not int
                    or record.get("schema_version") != 1 or record.get("dropped")
                    or record.get("injected")):
                first_failure = _failure(frame_ordinal=rows, source_sequence=sequence if type(sequence) is int else -1,
                    connection_ordinal=0, direction=str(direction), aggregate_message_ordinal=0,
                    command_id=0, parser_stage="frame_metadata",
                    bounded_error_code="FRAME_METADATA_INVALID")
                break
            ordinal = connections.setdefault(connection, len(connections) + 1)
            if connection in last_sequence:
                if sequence <= last_sequence[connection]:
                    first_failure = _failure(frame_ordinal=rows, source_sequence=sequence,
                        connection_ordinal=ordinal, direction=direction, aggregate_message_ordinal=0,
                        command_id=0, parser_stage="transport_sequence",
                        bounded_error_code="FRAME_SEQUENCE_REPEATED")
                    break
                if sequence != last_sequence[connection] + 1:
                    first_failure = _failure(frame_ordinal=rows, source_sequence=sequence,
                        connection_ordinal=ordinal, direction=direction, aggregate_message_ordinal=0,
                        command_id=0, parser_stage="transport_sequence",
                        bounded_error_code="FRAME_SEQUENCE_GAP")
                    break
            last_sequence[connection] = sequence
            if direction == "client":
                # Do not access payload_b64 or decode client request bytes.
                continue
            try:
                count = record.get("payload_bytes")
                encoded = record.get("payload_b64")
                if type(count) is not int or not 0 <= count <= MAX_PAYLOAD_BYTES or type(encoded) is not str:
                    raise ValueError
                payload = base64.b64decode(encoded, validate=True)
                if len(payload) != count or hashlib.sha256(payload).hexdigest() != record.get("payload_sha256"):
                    raise ValueError
            except (ValueError, TypeError, binascii.Error, OverflowError):
                first_failure = _failure(frame_ordinal=rows, source_sequence=sequence,
                    connection_ordinal=ordinal, direction=direction, aggregate_message_ordinal=0,
                    command_id=0, parser_stage="frame_metadata",
                    bounded_error_code="FRAME_METADATA_INVALID")
                break
            try:
                bundle = live.extract_bundle(payload)
            except (ValueError, TypeError):
                first_failure = _failure(frame_ordinal=rows, source_sequence=sequence,
                    connection_ordinal=ordinal, direction=direction, aggregate_message_ordinal=0,
                    command_id=0, parser_stage="aggregate_envelope",
                    bounded_error_code="AGGREGATE_ENVELOPE_INVALID")
                break
            if bundle is None:
                continue
            for message_ordinal, item in enumerate(bundle[1], 1):
                if item.get("type") != live.PB_RECORD_TYPE:
                    continue
                command = 0
                try:
                    if item.get("version") != 2:
                        raise RuntimeError("version")
                    raw = item.get("payload")
                    if not isinstance(raw, bytes):
                        raise ValueError
                    outer = wire.fields(raw)
                    present, value = wire.optional_scalar(outer, 1)
                    if not present or type(value) is not int:
                        first_failure = _failure(frame_ordinal=rows, source_sequence=sequence,
                            connection_ordinal=ordinal, direction=direction,
                            aggregate_message_ordinal=message_ordinal, command_id=0,
                            parser_stage="aggregate_message",
                            bounded_error_code="COMMAND_REQUIRED_FIELD_MISSING",
                            required_field_ids=(1,))
                        break
                    command = value
                    variant = live.COMMAND_VARIANTS.get(command)
                    if variant is None:
                        first_failure = _failure(frame_ordinal=rows, source_sequence=sequence,
                            connection_ordinal=ordinal, direction=direction,
                            aggregate_message_ordinal=message_ordinal, command_id=command,
                            parser_stage="aggregate_message", bounded_error_code="COMMAND_UNKNOWN")
                        break
                    wrapper = wire.fields(wire.one_blob(outer, 3))
                    if set(wrapper) != {variant}:
                        first_failure = _failure(frame_ordinal=rows, source_sequence=sequence,
                            connection_ordinal=ordinal, direction=direction,
                            aggregate_message_ordinal=message_ordinal, command_id=command,
                            parser_stage="aggregate_message",
                            bounded_error_code="AGGREGATE_ENVELOPE_INVALID")
                        break
                    group = wire.fields(wire.one_blob(wrapper, variant))
                    code, fields, facts = _command_problem(command, group, wire)
                    if code:
                        first_failure = _failure(frame_ordinal=rows, source_sequence=sequence,
                            connection_ordinal=ordinal, direction=direction,
                            aggregate_message_ordinal=message_ordinal, command_id=command,
                            parser_stage="command_contract", bounded_error_code=code,
                            required_field_ids=fields, missing_fact_codes=facts)
                        break
                    # Confirm the production parser agrees after bounded structural checks.
                    live.parse_command(command, group, live.SourceMeta(sequence=sequence,
                        connection_id="diagnostic", message_index=rows,
                        sub_index=message_ordinal - 1))
                except RuntimeError:
                    first_failure = _failure(frame_ordinal=rows, source_sequence=sequence,
                        connection_ordinal=ordinal, direction=direction,
                        aggregate_message_ordinal=message_ordinal, command_id=command,
                        parser_stage="aggregate_message",
                        bounded_error_code="PROTOCOL_VERSION_UNSUPPORTED")
                    break
                except (ValueError, TypeError, KeyError, OverflowError):
                    first_failure = _failure(frame_ordinal=rows, source_sequence=sequence,
                        connection_ordinal=ordinal, direction=direction,
                        aggregate_message_ordinal=message_ordinal, command_id=command,
                        parser_stage="command_contract",
                        bounded_error_code="COMMAND_SEMANTICS_UNSUPPORTED")
                    break
            if first_failure is not None:
                break
    after_hash, after_bytes = _sha256(path)
    after_directory = _directory_metadata(path.parent)
    if (after_hash, after_bytes, after_directory) != (before_hash, source_bytes, before_directory):
        first_failure = _failure(frame_ordinal=0, source_sequence=-1, connection_ordinal=0,
            direction="unknown", aggregate_message_ordinal=0, command_id=0,
            parser_stage="capture_complete", bounded_error_code="INTERNAL_DIAGNOSTIC_REJECTED")
    total_rows, total_directions, total_connections = _capture_metadata(path)
    sidecar = _sidecar_status(path, total_rows, total_directions)
    if first_failure is None:
        first_failure = _failure(frame_ordinal=0, source_sequence=-1, connection_ordinal=0,
            direction="unknown", aggregate_message_ordinal=0, command_id=0,
            parser_stage="capture_complete", bounded_error_code="CAPTURE_INCOMPLETE",
            missing_fact_codes=("TOKEN_STEP_UNAVAILABLE", "COMMITTED_STEP_UNAVAILABLE",
                                "FOUR_OBSERVER_HISTORY_UNAVAILABLE"))
    facts_present = not bool(first_failure["required_field_ids"])
    authoritative_contract = first_failure["bounded_error_code"] not in {
        "COMMAND_SEMANTICS_UNSUPPORTED", "DECLARATION_FACTS_INCOMPLETE",
        "EXCHANGE_FACTS_INCOMPLETE", "PROTOCOL_VERSION_UNSUPPORTED",
    }
    local_repair_allowed = False
    fresh_recapture_required = (bool(first_failure["required_field_ids"])
        or first_failure["bounded_error_code"] in {
            "FRAME_SEQUENCE_REPEATED", "FRAME_SEQUENCE_GAP", "FRAME_CONNECTION_SWITCH_UNSAFE",
            "AGGREGATE_MESSAGE_TRUNCATED", "CAPTURE_INCOMPLETE",
        } or sidecar != "LEGACY_CLOSED_SIDECAR_VERIFIED")
    return {
        "capture_id": entry["capture_id"],
        "source_sha256": before_hash,
        "source_bytes": source_bytes,
        "source_rows": total_rows,
        "direction_counts": dict(sorted(total_directions.items())),
        "connection_count": total_connections,
        **first_failure,
        "closed_sidecar_status": sidecar,
        "facts_present": facts_present,
        "authoritative_contract_present": authoritative_contract,
        "local_repair_allowed": local_repair_allowed,
        "fresh_recapture_required": fresh_recapture_required,
    }


def _validate_output(value: object) -> None:
    forbidden = {"payload", "payload_b64", "cards", "hand", "state", "action", "history",
                 "authorization", "cookie", "cookies", "hidden_opponent_hands"}
    def walk(item: object) -> None:
        if item is None or type(item) in {bool, int, float}:
            return
        if isinstance(item, str):
            if len(item) > 256 or "\\" in item or "/" in item or "\r" in item or "\n" in item:
                raise ValueError("unsafe_output")
            return
        if isinstance(item, list):
            if len(item) > 256:
                raise ValueError("unsafe_output")
            for child in item: walk(child)
            return
        if isinstance(item, dict):
            if forbidden.intersection(str(key).lower() for key in item):
                raise ValueError("unsafe_output")
            for child in item.values(): walk(child)
            return
        raise ValueError("unsafe_output")
    walk(value)


def main() -> int:
    selectors = json.loads(MANIFEST.read_text(encoding="utf-8"))
    ids = [entry["capture_id"] for entry in selectors]
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--capture-id", action="append", choices=ids)
    args = parser.parse_args()
    root = args.root.resolve()
    selected = set(args.capture_id or ids)
    try:
        if root != ROOT.resolve():
            raise ValueError("unsafe_root")
        results = [diagnose_capture(entry, root) for entry in selectors
                   if entry["capture_id"] in selected]
        value = {"schema": SCHEMA, "status": "blocked", "captures": results}
        _validate_output(value)
        print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 2
    except Exception:
        value = {"schema": SCHEMA, "status": "blocked", "captures": [],
                 "bounded_error_code": "INTERNAL_DIAGNOSTIC_REJECTED"}
        print(json.dumps(value, sort_keys=True, separators=(",", ":")))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
