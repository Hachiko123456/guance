"""TD-02/03/04/09: synthetic-only offline reader/converter acceptance.

All I/O stays in tmp_path. No old recordings, network, or private model files.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from guance.contracts import SourceFrame
from guance.domain.cards import full_deck
from guance.domain.session import SessionEngine
from guance.infrastructure.protocol.live import GAME_HOST, parse_aggregate
from guance.infrastructure.training_data import legacy
from guance.infrastructure.training_data.legacy import convert_legacy, read_legacy_episodes
from guance.infrastructure.training_data.validation import validate_decision
from tests.training_data.test_derive import synthetic_updates


PRIVATE_SENTINEL = "SYNTHETIC_PRIVATE_SENTINEL"
FORBIDDEN = {"payload", "payload_b64", "raw_payload", "raw", "authorization", "cookie",
             "cookies", "remaining_hands", "hidden_opponent_hands", "ca_private_key"}


def _envelope(payload, sequence=1, direction="server"):
    return {
        "host": GAME_HOST, "direction": direction, "schema_version": 1,
        "payload_bytes": len(payload), "payload_b64": base64.b64encode(payload).decode("ascii"),
        "payload_sha256": hashlib.sha256(payload).hexdigest(), "received_at": float(sequence),
        "sequence": sequence, "connection_id": "synthetic-connection",
    }


def _write_source(tmp_path, rows, name="frames.jsonl"):
    directory = tmp_path / "input"
    directory.mkdir(exist_ok=True)
    path = directory / name
    path.write_bytes(b"".join((json.dumps(row, sort_keys=True) + "\n").encode() for row in rows))
    return path


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe(value, source):
    def keys(item):
        if isinstance(item, dict):
            for key, child in item.items():
                yield key
                yield from keys(child)
        elif isinstance(item, list):
            for child in item:
                yield from keys(child)
    assert len(FORBIDDEN.intersection(keys(value))) == 0
    text = json.dumps(value, sort_keys=True)
    # Count-only failure diagnostics; never dump exported contents to a log.
    leaks = sum(token in text for token in (PRIVATE_SENTINEL, str(source), source.as_posix(),
                                            "-----BEGIN PRIVATE KEY-----", "Bearer synthetic-secret"))
    assert leaks == 0, "safe conversion output leaked synthetic private content"


def _files(root):
    return {path.relative_to(root).as_posix(): _hash(path)
            for path in root.rglob("*") if path.is_file()}


@pytest.fixture
def synthetic_engine(monkeypatch):
    """Isolate envelope/allowlist tests from protocol tests below."""
    seen = []

    class Engine:
        def __init__(self, session_id, **_kwargs):
            self.paused = False
            self.session_id = session_id

        def feed(self, frame):
            if frame.direction == "client":
                assert frame.payload == b""
                return []
            assert frame.direction == "server"
            seen.append(frame.sequence)
            update = deepcopy(synthetic_updates()[frame.sequence - 1])
            update.data["table"]["session_id"] = self.session_id
            update.data.update(raw_payload=PRIVATE_SENTINEL, authorization="Bearer synthetic-secret")
            update.data["table"].update(remaining_hands=[[PRIVATE_SENTINEL]], cookie=PRIVATE_SENTINEL,
                                         ca_private_key="-----BEGIN PRIVATE KEY-----")
            return [update]

    monkeypatch.setattr(legacy, "SessionEngine", Engine)
    return seen


def _synthetic_rows():
    return [_envelope(f"synthetic-{i}".encode(), i) for i in range(1, 6)]



def test_iter_jsonl_frames_preserves_client_transport_sequence_without_request_bytes(tmp_path):
    rows = [
        _envelope(b"server-one", sequence=1, direction="server"),
        _envelope(b"client-secret", sequence=2, direction="client"),
        _envelope(b"server-two", sequence=3, direction="server"),
    ]
    rows[1].update(payload_b64="%%%not-read%%%", payload_sha256="not-read",
                   cookie=PRIVATE_SENTINEL)
    source = _write_source(tmp_path, rows)
    counters = {"input_rows": 0, "server_frames": 0, "ignored_client": 0,
                "ignored_non_target": 0}
    frames = list(legacy.iter_jsonl_frames(source, counters))
    assert [(frame.sequence, frame.direction) for frame in frames] == [
        (1, "server"), (2, "client"), (3, "server")
    ]
    assert frames[1].payload == b""
    assert counters == {"input_rows": 3, "server_frames": 2, "ignored_client": 1,
                        "ignored_non_target": 0}

def test_read_legacy_episodes_is_immutable_client_ignoring_and_allowlisted(tmp_path, synthetic_engine):
    client = _envelope(b"synthetic-client", sequence=99, direction="client")
    client.update(payload_b64="%%%bad-base64%%%", payload_sha256="bad", cookie=PRIVATE_SENTINEL)
    rows = _synthetic_rows()
    rows.insert(2, client)
    rows.insert(3, {"host": "not-the-authorized-game.invalid", "cookie": PRIVATE_SENTINEL})
    source = _write_source(tmp_path, rows)
    before_hash = _hash(source)
    before_mtime = source.stat().st_mtime_ns

    episodes, report = read_legacy_episodes(source)

    assert synthetic_engine == [1, 2, 3, 4, 5]
    assert report["status"] == "converted"
    assert report["counters"] == {"input_rows": 7, "server_frames": 5,
                                  "ignored_client": 1, "ignored_non_target": 1}
    assert (report["episodes"], report["decisions"], report["accepted_candidates"]) == (1, 1, 1)
    assert report["source_sha256"] == before_hash
    assert (_hash(source), source.stat().st_mtime_ns) == (before_hash, before_mtime)
    validate_decision(episodes[0]["records"][0])
    _safe({"episodes": episodes, "report": report}, source)


def test_convert_legacy_stable_by_content_hash_not_path_and_repeat_is_idempotent(tmp_path, synthetic_engine):
    source = _write_source(tmp_path, _synthetic_rows())
    copy_dir = tmp_path / "renamed-input"
    copy_dir.mkdir()
    copy_path = copy_dir / "different-name.jsonl"
    copy_path.write_bytes(source.read_bytes())
    before = (_hash(source), _hash(copy_path))
    first, second = tmp_path / "output-a", tmp_path / "output-b"
    first_report = convert_legacy(source, first)
    expected_files = _files(first)
    second_report = convert_legacy(copy_path, second)
    repeated_report = convert_legacy(source, first)

    assert first_report == second_report == repeated_report
    assert expected_files == _files(second) == _files(first)
    assert len(expected_files) == 2
    assert (_hash(source), _hash(copy_path)) == before
    assert not list(first.rglob("*.tmp"))
    episode_paths = list((first / "training_data" / "episodes").glob("*.jsonl"))
    manifest_paths = list((first / "training_data" / "manifests").glob("*.json"))
    assert len(episode_paths) == len(manifest_paths) == 1
    manifest = json.loads(manifest_paths[0].read_text(encoding="utf-8"))
    assert manifest["source_id"] == "sha256:" + before[0]
    assert manifest["status"] == "closed"
    record = json.loads(episode_paths[0].read_text(encoding="utf-8"))
    validate_decision(record)
    _safe([manifest, record, first_report], source)
    _safe([manifest, record], copy_path)


@pytest.mark.parametrize("failure,reason", [
    ("missing-direction", "frame_direction_unknown"),
    ("unknown-direction", "frame_direction_unknown"),
    ("bad-hash", "payload_integrity_mismatch"),
    ("bad-size", "payload_integrity_mismatch"),
    ("bad-base64", "invalid_frame_metadata_or_base64"),
    ("unsupported-schema", "invalid_frame_metadata_or_base64"),
    ("injected", "modified_or_dropped_frame"),
    ("dropped", "modified_or_dropped_frame"),
    ("truncated", "invalid_json_or_truncated_line"),
])
def test_reader_and_converter_reject_bad_envelopes_without_changing_source(tmp_path, failure, reason):
    row = _envelope(b"synthetic-payload")
    if failure == "missing-direction":
        row.pop("direction")
    elif failure == "unknown-direction":
        row["direction"] = "unknown"
    elif failure == "bad-hash":
        row["payload_sha256"] = "0" * 64
    elif failure == "bad-size":
        row["payload_bytes"] += 1
    elif failure == "bad-base64":
        row["payload_b64"] = "%%%invalid%%%"
    elif failure == "unsupported-schema":
        row["schema_version"] = 99
    elif failure in {"injected", "dropped"}:
        row[failure] = True
    source = _write_source(tmp_path, [row])
    if failure == "truncated":
        source.write_bytes(b'{"host":')
    before = (_hash(source), source.stat().st_mtime_ns)

    episodes, report = read_legacy_episodes(source)
    converted_report = convert_legacy(source, tmp_path / "output")

    assert episodes == []
    assert report == converted_report
    assert report["status"] == "rejected"
    assert report["errors"] == [{"code": reason, "line": 1}]
    assert report["decisions"] == 0
    assert (_hash(source), source.stat().st_mtime_ns) == before
    manifest_path, = (tmp_path / "output" / "training_data" / "manifests").glob("*.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "degraded"
    assert manifest["episodes"] == []
    _safe([report, manifest], source)


def test_truncated_tail_quarantines_previous_decision_and_removes_outcome(tmp_path, synthetic_engine):
    source = _write_source(tmp_path, _synthetic_rows()[:-1])
    with source.open("ab") as stream:
        stream.write(b'{"unfinished":')
    before = _hash(source)
    episodes, report = read_legacy_episodes(source)
    assert report["status"] == "rejected"
    assert report["quarantined"] == 1
    assert report["accepted_candidates"] == 0
    assert report["errors"] == [{"code": "invalid_json_or_truncated_line", "line": 5}]
    record = episodes[0]["records"][0]
    validate_decision(record)
    assert record["quality"]["lost"] is True
    assert record["outcome"] is None
    assert record["reward"] is None
    assert _hash(source) == before


def test_converter_refuses_to_write_within_source_directory(tmp_path, synthetic_engine):
    source = _write_source(tmp_path, _synthetic_rows())
    before = _files(source.parent)
    with pytest.raises(ValueError, match="^output_must_be_outside_source_directory$"):
        convert_legacy(source, source.parent / "output")
    assert _files(source.parent) == before


# Minimal synthetic protobuf/aggregate framing, independently built rather than
# copying any real recording or monkeypatching the parser in this test.
def _varint(value):
    encoded = bytearray()
    while value >= 128:
        encoded.append((value & 127) | 128)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _field(number, value):
    if isinstance(value, bytes):
        return _varint(number * 8 + 2) + _varint(len(value)) + value
    return _varint(number * 8) + _varint(value)


def _packet(command, variant, body):
    message = _field(1, command) + _field(3, _field(variant, body))
    sub = ((16 + len(message)).to_bytes(2, "big") + (15815).to_bytes(2, "big")
           + (2).to_bytes(2, "big") + bytes(8) + (len(message) + 2).to_bytes(4, "big") + message)
    body = (1).to_bytes(2, "big") + sub
    aggregate = (len(body) + 2).to_bytes(2, "big") + body
    raw = bytearray(24 + len(aggregate))
    raw[8:10] = b"\xff\xfd"
    raw[24:] = aggregate
    raw[:2] = len(raw).to_bytes(2, "big")
    return bytes(raw)


def test_real_parser_and_engine_reconstruction_match_converted_own_decision(tmp_path):
    hand = full_deck()[:27]
    payloads = [
        _packet(3001, 2, _field(1, 27) + _field(2, b"".join(map(_varint, hand))) + _field(4, 5)),
        _packet(3004, 5, _field(1, 0) + _field(2, 20) + _field(3, 2) + _field(4, 35)),
    ]
    for turn in range(2):
        payloads.append(_packet(3005, 6, _field(2, 0) + _field(3, 3) + _field(4, 1)
                                + _field(5, 26 - turn) + _field(6, 1) + _field(7, _varint(hand[turn]))
                                + _field(8, 1) + _field(9, 1) + _field(11, b"x" * 20)))
        if turn == 0:
            for player in (3, 2, 1):
                reset = _field(4, 1) if player == 1 else b""
                payloads.append(_packet(3006, 7, _field(2, player) + _field(3, player - 1)
                                        + reset + _field(5, 1)))
    rows = [_envelope(payload, index) for index, payload in enumerate(payloads, 1)]
    source = _write_source(tmp_path, rows)
    digest = _hash(source)
    reference = SessionEngine("legacy-" + digest[:24], aggregate_parser=parse_aggregate, accepted_host=GAME_HOST)
    observations, states = [], []
    for index, payload in enumerate(payloads, 1):
        updates = reference.feed(SourceFrame(host=GAME_HOST, payload=payload,
                                             received_at=float(index), sequence=index,
                                             connection_id="synthetic-connection", direction="server"))
        assert not reference.paused, "synthetic protocol fixture was rejected"
        observations.extend(update.observation for update in updates if update.kind == "observation")
        states.extend(update.data["table"] for update in updates if update.kind == "state")
    episodes, report = read_legacy_episodes(source)
    assert report["status"] == "converted"
    assert (report["decisions"], report["partial"], report["accepted_candidates"]) == (1, 1, 0)
    record = episodes[0]["records"][0]
    validate_decision(record)
    # Engine also emits observations AFTER our action, for the next actor.
    # The oracle is the last independently observed OWN turn, not the last update.
    expected = [o for o in observations if o.current_seat == o.my_seat][-1].to_dict()
    assert expected["state_version"] < observations[-1].state_version
    # Digest comparisons keep cards and payloads out of failure logs/evidence.
    for key in ("session_id", "game_id", "state_version", "level", "my_seat", "first_seat",
                "current_seat", "hand", "remaining", "history", "free_lead", "trick"):
        actual_digest = hashlib.sha256(json.dumps(record["pre_state"][key], sort_keys=True).encode()).hexdigest()
        expected_digest = hashlib.sha256(json.dumps(expected[key], sort_keys=True).encode()).hexdigest()
        assert actual_digest == expected_digest, "converted pre-state differs from independent engine"
    expected_action = states[-1]["history"][-1]
    actual_action = {key: record["observed_action"][key] for key in expected_action}
    assert hashlib.sha256(json.dumps(actual_action, sort_keys=True).encode()).digest() == hashlib.sha256(
        json.dumps(expected_action, sort_keys=True).encode()).digest()
    assert record["outcome"] is None
    assert episodes[0]["provenance_only_actions"] == 4
    assert _hash(source) == digest
    converted = convert_legacy(source, tmp_path / "output")
    assert converted == report
    _safe([episodes, report], source)
