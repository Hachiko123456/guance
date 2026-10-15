"""Read-only replay support for historical Guance debug captures.

Client payload bytes are never decoded.  Server payloads are verified and
parsed only in memory.  The module does not open sockets, alter proxies, replay
requests, or write into capture directories.
"""
from __future__ import annotations

import base64
import binascii
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Iterable, Iterator

from guance.contracts import SourceFrame
from guance.domain.events import DealEvent, SettlementEvent, SourceMeta, StartEvent
from guance.domain.session import SessionEngine
from guance.infrastructure.capture.live_subset import NormalExchangeLiveSubsetProvider
from guance.infrastructure.protocol import live, wire

MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_PAYLOAD_BYTES = 1024 * 1024


class OfflineReplayError(ValueError):
    """Bounded replay failure that never embeds untrusted capture text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ReplayFrame:
    row: int
    epoch: int
    frame: SourceFrame


def sha256_file(path: str | Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _record(line: bytes) -> dict[str, object]:
    if len(line) > MAX_LINE_BYTES:
        raise OfflineReplayError("FRAME_METADATA_INVALID")
    try:
        value = json.loads(line.decode("utf-8-sig"))
    except (UnicodeError, ValueError):
        raise OfflineReplayError("FRAME_METADATA_INVALID") from None
    if not isinstance(value, dict):
        raise OfflineReplayError("FRAME_METADATA_INVALID")
    return value


def _metadata(record: dict[str, object]) -> tuple[str, str, int, float, str]:
    direction = record.get("direction")
    connection = record.get("connection_id")
    sequence = record.get("sequence")
    received = record.get("received_at")
    host = record.get("host")
    if (direction not in {"client", "server"} or type(connection) is not str
            or not connection or type(sequence) is not int or not 0 <= sequence < 2**63
            or type(received) not in {int, float} or type(host) is not str
            or record.get("schema_version") != 1):
        if direction is None:
            raise OfflineReplayError("DIRECTION_UNAVAILABLE")
        raise OfflineReplayError("FRAME_METADATA_INVALID")
    return direction, connection, sequence, float(received), host


def _server_payload(record: dict[str, object]) -> bytes:
    encoded = record.get("payload_b64")
    count = record.get("payload_bytes")
    digest = record.get("payload_sha256")
    if type(encoded) is not str or type(count) is not int or not 0 <= count <= MAX_PAYLOAD_BYTES:
        raise OfflineReplayError("FRAME_INTEGRITY_INVALID")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise OfflineReplayError("FRAME_INTEGRITY_INVALID") from None
    if len(payload) != count or hashlib.sha256(payload).hexdigest() != digest:
        raise OfflineReplayError("FRAME_INTEGRITY_INVALID")
    return payload


def iter_debug_frames(path: str | Path) -> Iterator[ReplayFrame]:
    """Yield strict frames while never accessing client payload fields."""
    connections: dict[str, int] = {}
    last_sequence: dict[str, int] = {}
    with Path(path).open("rb") as stream:
        for row, line in enumerate(stream, 1):
            record = _record(line)
            direction, connection, sequence, received, host = _metadata(record)
            epoch = connections.setdefault(connection, len(connections) + 1)
            previous = last_sequence.get(connection)
            if previous is not None:
                if sequence <= previous:
                    raise OfflineReplayError("FRAME_SEQUENCE_REPEATED")
                if sequence != previous + 1:
                    raise OfflineReplayError("FRAME_SEQUENCE_GAP")
            last_sequence[connection] = sequence
            payload = _server_payload(record) if direction == "server" else b""
            yield ReplayFrame(row, epoch, SourceFrame(
                host=host, payload=payload, received_at=received,
                connection_id=connection, sequence=sequence, direction=direction,
            ))


def _shape(group: dict[int, list[tuple[int, int | bytes]]]) -> str:
    return ",".join(f"{number}:{wire_type}" for number in sorted(group)
                    for wire_type, _value in group[number])


def _safe_scalar_fields(command: int) -> frozenset[int]:
    return {
        3004: frozenset({1, 2, 3, 4}),
        3005: frozenset({1, 2, 3, 4, 5, 6, 8, 9}),
        3006: frozenset({2, 3, 4, 5}),
    }.get(command, frozenset())


def protocol_variant_matrix(capture_id: str, path: str | Path) -> dict[str, object]:
    """Return structural variants only; no payloads, cards, hands, or paths."""
    source = Path(path)
    digest, size = sha256_file(source)
    variants: dict[tuple[int, int, int, str], dict[str, object]] = {}
    directions: Counter[str] = Counter()
    rows = 0
    direction_available = True
    first_rejection: dict[str, object] | None = None
    connections: dict[str, int] = {}
    last: dict[str, int] = {}
    with source.open("rb") as stream:
        for row, line in enumerate(stream, 1):
            rows = row
            try:
                record = _record(line)
                direction = record.get("direction")
                if direction is None:
                    directions["unavailable"] += 1
                    direction_available = False
                    continue
                direction, connection, sequence, _received, host = _metadata(record)
                directions[direction] += 1
                epoch = connections.setdefault(connection, len(connections) + 1)
                previous = last.get(connection)
                if previous is not None and sequence != previous + 1:
                    code = "FRAME_SEQUENCE_REPEATED" if sequence <= previous else "FRAME_SEQUENCE_GAP"
                    first_rejection = first_rejection or {
                        "row": row, "epoch": epoch, "sequence": sequence, "command": 0,
                        "code": code,
                    }
                last[connection] = sequence
                if direction != "server" or host != live.GAME_HOST:
                    continue
                payload = _server_payload(record)
                try:
                    bundle = live.extract_bundle(payload)
                except (ValueError, TypeError):
                    first_rejection = first_rejection or {
                        "row": row, "epoch": epoch, "sequence": sequence, "command": 0,
                        "code": "AGGREGATE_ENVELOPE_INVALID",
                    }
                    continue
                if bundle is None:
                    continue
                for item in bundle[1]:
                    if item.get("type") != live.PB_RECORD_TYPE:
                        continue
                    command = 0
                    try:
                        version = item.get("version")
                        raw = item.get("payload")
                        if version != 2 or not isinstance(raw, bytes):
                            raise OfflineReplayError("PROTOCOL_VERSION_UNSUPPORTED")
                        outer = wire.fields(raw)
                        command = wire.scalar(outer, 1)
                        variant = live.COMMAND_VARIANTS.get(command)
                        if variant is None:
                            raise OfflineReplayError("COMMAND_UNKNOWN")
                        wrapper = wire.fields(wire.one_blob(outer, 3))
                        if set(wrapper) != {variant}:
                            raise OfflineReplayError("WRAPPER_VARIANT_MISMATCH")
                        group = wire.fields(wire.one_blob(wrapper, variant))
                        group_shape = _shape(group)
                        key = (command, version, variant, group_shape)
                        entry = variants.setdefault(key, {
                            "capture_id": capture_id, "command_id": command,
                            "record_version": version, "wrapper_variant": variant,
                            "group_shape": group_shape, "occurrence_count": 0,
                            "field_wire_types": {}, "safe_scalar_domains": {},
                            "first_sequence": sequence, "last_sequence": sequence,
                            "parser_expected_result": "converted",
                            "support_classification": "supported_subset",
                        })
                        entry["occurrence_count"] = int(entry["occurrence_count"]) + 1
                        entry["last_sequence"] = sequence
                        entry["field_wire_types"] = {
                            str(number): sorted({wire_type for wire_type, _value in values})
                            for number, values in sorted(group.items())
                        }
                        safe = _safe_scalar_fields(command)
                        domains = entry["safe_scalar_domains"]
                        assert isinstance(domains, dict)
                        for number in safe:
                            values = [value for wire_type, value in group.get(number, [])
                                      if wire_type == 0 and type(value) is int]
                            if values:
                                current = set(domains.get(str(number), []))
                                current.update(int(value) for value in values)
                                domains[str(number)] = sorted(current)[:32]
                        try:
                            event = live.parse_command(command, group, SourceMeta(
                                sequence=sequence, connection_id=f"epoch-{epoch}",
                                message_index=row, sub_index=int(item.get("sub_index", 0)),
                                record_type=live.PB_RECORD_TYPE, record_version=2,
                                command_variant=variant, group_shape=group_shape,
                            ))
                            if event.__class__.__name__ == "InformationalEvent":
                                entry["parser_expected_result"] = "informational"
                        except (ValueError, TypeError):
                            entry["parser_expected_result"] = "expected_unsupported"
                            entry["support_classification"] = "protocol_variant_unsupported"
                            first_rejection = first_rejection or {
                                "row": row, "epoch": epoch, "sequence": sequence,
                                "command": command, "code": "PROTOCOL_VARIANT_UNSUPPORTED",
                            }
                    except OfflineReplayError as exc:
                        first_rejection = first_rejection or {
                            "row": row, "epoch": epoch, "sequence": sequence,
                            "command": command, "code": exc.code,
                        }
                    except (ValueError, TypeError, KeyError, OverflowError):
                        first_rejection = first_rejection or {
                            "row": row, "epoch": epoch, "sequence": sequence,
                            "command": command, "code": "AGGREGATE_MESSAGE_INVALID",
                        }
            except OfflineReplayError as exc:
                first_rejection = first_rejection or {
                    "row": row, "epoch": 0, "sequence": -1, "command": 0,
                    "code": exc.code,
                }
    return {
        "capture_id": capture_id, "source_sha256": digest, "source_bytes": size,
        "source_rows": rows, "direction_field_available": direction_available,
        "direction_counts": dict(sorted(directions.items())),
        "connection_epochs": len(connections), "client_payload_decoded": False,
        "variants": [variants[key] for key in sorted(variants)],
        "first_parser_rejection": first_rejection,
    }


def strict_replay(capture_id: str, path: str | Path, *,
                  rules_source_root: str | Path | None = None) -> dict[str, object]:
    """Replay one directed capture through the production parser/session path."""
    provider = NormalExchangeLiveSubsetProvider(rules_source_root)
    provider.bind_capture(capture_id)
    engine = SessionEngine(
        "offline-replay", aggregate_parser=live.parse_aggregate,
        accepted_host=live.GAME_HOST, event_adapter=provider.adapt_event,
        event_observer=provider.observe_applied, reset_observer=provider.reset_session,
    )
    errors: Counter[str] = Counter()
    decisions = []
    event_counts: Counter[str] = Counter()
    epochs: dict[int, dict[str, object]] = {}
    try:
        for replay in iter_debug_frames(path):
            epoch = epochs.setdefault(replay.epoch, {
                "epoch": replay.epoch, "deal_count": 0, "start_count": 0,
                "settlement_count": 0, "first_observed_game_previous_result": "unknown",
                "model_state": "unavailable", "unsupported_code": "PREVIOUS_RESULT_UNAVAILABLE",
            })
            if replay.frame.direction == "server":
                try:
                    parsed = live.parse_aggregate(replay.frame.payload, SourceMeta(
                        received_at=replay.frame.received_at, sequence=replay.frame.sequence,
                        connection_id=replay.frame.connection_id,
                        message_index=replay.frame.sequence,
                    ))
                    for event in parsed:
                        event_counts[type(event).__name__] += 1
                        if isinstance(event, DealEvent):
                            epoch["deal_count"] = int(epoch["deal_count"]) + 1
                        elif isinstance(event, StartEvent):
                            epoch["start_count"] = int(epoch["start_count"]) + 1
                        elif isinstance(event, SettlementEvent):
                            epoch["settlement_count"] = int(epoch["settlement_count"]) + 1
                except (ValueError, TypeError):
                    errors["protocol_rejected"] += 1
            updates = engine.feed(replay.frame)
            for update in updates:
                if update.kind == "error":
                    errors[update.error_code or "LIVE_STATE_UNAVAILABLE"] += 1
            decisions.extend(provider.drain_decisions())
    except OfflineReplayError as exc:
        errors[exc.code] += 1
    summary = []
    for index, decision in enumerate(decisions, 1):
        history = decision.history.identity
        rows = decision.action_session.legal_rows
        identity = {
            "index": index, "source_sequence": decision.source_sequence,
            "game_id": decision.public_state.identity.game_id,
            "state_version": decision.public_state.identity.state_version,
            "observer_seat": decision.marvel_state.observer_seat,
            "phase": decision.action_session.phase,
            "token_step": history.token_step, "committed_step": history.committed_step,
            "history_sha256": decision.history.history_sha256,
            "legal_row_count": len(rows.rows),
            "legal_rows_generation": rows.rows[0].generation if rows.rows else "",
            "cache_request_id": decision.cache_request.request_id,
        }
        identity["decision_identity_sha256"] = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        summary.append(identity)
    return {
        "capture_id": capture_id, "protocol_identity": live.PROTOCOL_IDENTITY,
        "protocol_rejected": errors.get("protocol_rejected", 0),
        "unexpected_unknown_command": errors.get("COMMAND_UNKNOWN", 0),
        "frame_integrity_errors": errors.get("FRAME_INTEGRITY_INVALID", 0),
        "sequence_errors": errors.get("FRAME_SEQUENCE_GAP", 0)
                           + errors.get("FRAME_SEQUENCE_REPEATED", 0),
        "engine_paused": engine.paused, "error_counts": dict(sorted(errors.items())),
        "event_counts": dict(sorted(event_counts.items())),
        "connection_epochs": [epochs[key] for key in sorted(epochs)],
        "supported_decision_count": len(summary), "supported_decisions": summary,
        "model_adapter_eligible_count": provider.model_adapter_eligible_count,
        "client_payload_decoded": False,
    }


__all__ = [
    "OfflineReplayError", "ReplayFrame", "iter_debug_frames",
    "protocol_variant_matrix", "sha256_file", "strict_replay",
]
