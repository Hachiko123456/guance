"""Synthetic-only acceptance for TD-02..TD-08; no Qt/network/live capture."""
from __future__ import annotations

from copy import deepcopy
import json
import threading
import time

import pytest

from guance.contracts import EngineUpdate
from guance.domain.cards import encode_card
from guance.infrastructure.training_data import TrainingDataCollector, TrainingDataRecorder
from guance.infrastructure.training_data.schema import action_signature, snapshot_update

CARD = encode_card(2, 2)
SECRET = "PRIVATE_SENTINEL_NOT_FOR_EXPORT"


def sequence(game="game-0001", session="test", confirmed=True):
    """Safe-table transitions; not a raw capture or full legal-game simulation."""
    deal = {
        "session_id": session, "game_id": game, "state_version": 1,
        "captured_at": 1.0, "phase": "waiting_start", "level": 2,
        "my_seat": -1, "first_seat": -1, "current_seat": -1,
        "hand": [CARD], "remaining": [1, 2, 0, 2], "history": [],
        "free_lead": True, "trick": 0, "seat_confirmed": False,
        "seat_confirmation_source": None, "finish_ranks_by_seat": [-1, -1, 1, -1],
    }
    start = deepcopy(deal)
    start.update(state_version=2, captured_at=2.0, phase="active", first_seat=0, current_seat=0)
    seat = deepcopy(start)
    seat.update(state_version=3, captured_at=3.0, my_seat=0, seat_confirmed=True, seat_confirmation_source="manual")
    action = {"index": 1, "player": 0, "kind": "play", "cards": [CARD], "combo_type": 1, "next_player": None, "trick": 0}
    post = deepcopy(seat)
    post.update(state_version=4 if confirmed else 3, captured_at=4.0, phase="ended", current_seat=-1,
                hand=[], remaining=[0, 2, 0, 2], history=[action], free_lead=False,
                finish_ranks_by_seat=[2, -1, 1, -1])
    settled = deepcopy(post)
    settled.update(state_version=post["state_version"] + 1, captured_at=5.0, phase="settled", finish_ranks_by_seat=[2, 3, 1, 4])
    pairs = [("deal", deal), ("start", start)]
    if confirmed:
        pairs.append(("manual-seat-confirmation", seat))
    pairs.extend([("action", post), ("settlement", settled)])
    return [EngineUpdate("state", message, data={"source_sequence": i + 1, "table": table}) for i, (message, table) in enumerate(pairs)]


def collect(updates):
    builder = TrainingDataCollector(source="synthetic", source_id="fixed-source")
    output = []
    for update in updates:
        output.extend(builder.observe_update(update))
    return builder, output


def wait_for(predicate, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("background worker did not reach expected synthetic state")


def records(root):
    return [json.loads(line) for p in (root / "training_data" / "episodes").glob("*.jsonl") for line in p.read_text(encoding="utf-8").splitlines()]


def manifest(root):
    paths = list((root / "training_data" / "manifests").glob("*.json"))
    return json.loads(paths[0].read_text(encoding="utf-8")) if paths else {}


def test_settlement_publishes_immediately_before_close_or_next_deal(tmp_path):
    recorder = TrainingDataRecorder(tmp_path, source="synthetic", source_id="immediate")
    try:
        for update in sequence():
            recorder.observe_update(update)
        wait_for(lambda: recorder.stats()["written_episodes"] == 1)
        actual = records(tmp_path)
        assert len(actual) == 1
        assert actual[0]["status"] == "accepted_candidate"
        assert actual[0]["outcome"] == {"phase": "settled", "finish_ranks_by_seat": [2, 3, 1, 4]}
        assert actual[0]["reward"] is None
        assert len(manifest(tmp_path)["episodes"]) == 1
        assert recorder.stats()["accepting"]
    finally:
        recorder.close(1)


def test_input_is_unchanged_and_previous_state_is_detached():
    updates = sequence()
    original = deepcopy(updates)
    builder = TrainingDataCollector(source="synthetic", source_id="frozen")
    for update in updates[:-2]:
        builder.observe_update(update)
    assert updates == original
    updates[-3].data["table"]["hand"].clear()
    builder.observe_update(updates[-2])
    episode = builder.observe_update(updates[-1])[0]
    assert episode["records"][0]["pre_state"]["hand"] == [CARD]
    assert original[-3].data["table"]["hand"] == [CARD]


def test_advice_candidate_and_nonstate_updates_are_not_actions():
    builder, output = collect(sequence()[:-2])
    assert output == []
    builder.observe_advice({"candidates": [{"cards": [CARD], "score": 999}], "payload": SECRET})
    assert builder.observe_update(EngineUpdate("candidate", data={"table": sequence()[-2].data["table"]})) == []
    assert builder.close()[0]["records"] == []


def test_ended_is_partial_without_outcome():
    builder, output = collect(sequence()[:-1])
    assert output == []
    record = builder.close()[0]["records"][0]
    assert record["status"] == "partial"
    assert record["outcome"] is None and record["reward"] is None
    assert "ended_without_settlement" in record["quality"]["reasons"]


def test_duplicate_settlement_is_idempotent_and_conflict_upserts(tmp_path):
    recorder = TrainingDataRecorder(tmp_path, source_id="upsert")
    updates = sequence()
    try:
        for update in updates:
            recorder.observe_update(update)
        wait_for(lambda: recorder.stats()["written_episodes"] == 1)
        episode_id = manifest(tmp_path)["episodes"][0]["episode_id"]
        duplicate = deepcopy(updates[-1])
        duplicate.data["table"]["captured_at"] = 9.0
        recorder.observe_update(duplicate)
        wait_for(lambda: recorder.stats()["processed_updates"] == len(updates) + 1)
        assert recorder.stats()["written_episodes"] == 1
        conflict = deepcopy(updates[-1])
        conflict.data["table"]["finish_ranks_by_seat"] = [2, 4, 1, 3]
        recorder.observe_update(conflict)
        wait_for(lambda: recorder.stats()["written_episodes"] == 2)
        assert records(tmp_path)[0]["status"] == "quarantine"
        assert records(tmp_path)[0]["outcome"] is None
        entries = manifest(tmp_path)["episodes"]
        assert len(entries) == 1 and entries[0]["episode_id"] == episode_id
        assert entries[0]["status"] == "quarantine"
    finally:
        recorder.close(1)


@pytest.mark.parametrize("change", ["version_gap", "index_gap", "history_conflict", "game_change"])
def test_ambiguous_linkage_invalidates_until_fresh_deal(change):
    updates = sequence()
    broken = deepcopy(updates[-2])
    if change == "version_gap":
        broken.data["table"]["state_version"] += 2
    elif change == "index_gap":
        broken.data["table"]["history"][0]["index"] = 2
    elif change == "history_conflict":
        broken.data["table"]["history"][0]["player"] = 1
    else:
        broken.data["table"]["game_id"] = "unannounced-game"
    builder, _ = collect(updates[:-2])
    builder.observe_update(broken)
    builder.observe_update(updates[-2])
    builder.observe_update(updates[-1])
    assert builder.stats()["active_invalidated"]
    old = builder.observe_update(sequence("game-0002")[0])
    assert old[0]["status"] == "quarantine"
    fresh = []
    for update in sequence("game-0002")[1:]:
        fresh.extend(builder.observe_update(update))
    assert fresh[-1]["status"] == "accepted_candidate"


def test_action_that_first_confirms_seat_is_provenance_only():
    builder, output = collect(sequence(confirmed=False))
    assert len(output) == 1 and output[0]["records"] == []
    assert output[0]["provenance_only_actions"] == 1
    assert builder.close() == []


def test_privacy_allowlist_excludes_nested_and_raw_private_fields(tmp_path):
    updates = sequence()
    for update in updates:
        update.data.update(payload=SECRET, authorization=SECRET)
        table = update.data["table"]
        table.update(raw=SECRET, remaining_hands=[[SECRET]], cookies=SECRET,
                     card_counter=[{"private": SECRET}], multiplier_evidence={"private": SECRET})
        for action in table["history"]:
            action["raw"] = SECRET
    recorder = TrainingDataRecorder(tmp_path)
    for update in updates:
        recorder.observe_update(update)
    recorder.observe_advice({"error_message": SECRET, "model_weights": SECRET, "score": float("nan")})
    recorder.observe_update(EngineUpdate("error", SECRET, data={"private": SECRET}))
    recorder.close(1)
    text = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "training_data").rglob("*.json*"))
    assert SECRET not in text
    assert "remaining_hands" not in text and "model_weights" not in text
    assert len(records(tmp_path)) == 1


def test_blocked_writer_queue_full_and_close_are_bounded(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    real_write = TrainingDataRecorder._write_episode
    def blocked(self, episode):
        entered.set()
        assert release.wait(5)
        real_write(self, episode)
    monkeypatch.setattr(TrainingDataRecorder, "_write_episode", blocked)
    recorder = TrainingDataRecorder(tmp_path, max_queue=8)
    try:
        for update in sequence():
            recorder.observe_update(update)
        assert entered.wait(2)
        begin = time.monotonic()
        for _ in range(100):
            recorder.observe_update(sequence()[-1])
        assert time.monotonic() - begin < 0.5
        assert recorder.stats()["dropped_updates"] > 0
        assert recorder.stats()["active_invalidated"]
        begin = time.monotonic()
        report = recorder.close(0.2)
        assert time.monotonic() - begin < 0.75
        assert report["writer_alive"] and not report["complete"]
        assert report["unflushed_records"] >= 1 and report["unflushed_updates"] > 0
    finally:
        release.set()
        recorder.close(1)
    assert records(tmp_path)[0]["status"] == "quarantine"
    assert len(manifest(tmp_path)["episodes"]) == 1
    assert not list((tmp_path / "training_data").rglob("*.tmp"))


def test_writer_error_reports_loss_without_raising(tmp_path, monkeypatch):
    def failed(self, episode):
        raise OSError(SECRET)
    monkeypatch.setattr(TrainingDataRecorder, "_write_episode", failed)
    recorder = TrainingDataRecorder(tmp_path)
    for update in sequence():
        recorder.observe_update(update)
    report = recorder.close(1)
    assert report["writer_errors"] > 0 and report["lost_records"] > 0
    assert not report["complete"]
    assert SECRET not in json.dumps(report)
    assert records(tmp_path) == []


def test_stable_source_identity_and_physical_card_signature():
    _, one = collect(sequence())
    _, two = collect(sequence())
    assert one == two
    action = sequence()[-2].data["table"]["history"][0]
    action["cards"] = [encode_card(2, 2), encode_card(2, 2, 1), encode_card(0, 14), encode_card(0, 15, 1)]
    reordered = deepcopy(action)
    reordered["cards"].reverse()
    assert action_signature(action) == action_signature(reordered)
    assert action["cards"] == [34, 290, 14, 271]


@pytest.mark.parametrize("key,value", [("captured_at", float("nan")), ("schema_version", 99)])
def test_invalid_schema_or_nonfinite_values_fail_closed(key, value):
    update = sequence()[0]
    update.data["table"][key] = value
    with pytest.raises(ValueError):
        snapshot_update(update)


def test_raw_sequence_gaps_are_not_action_gaps():
    updates = sequence()
    for index, update in enumerate(updates):
        update.data["source_sequence"] = 100 + index * 20
    _, output = collect(updates)
    assert output[0]["status"] == "accepted_candidate"

def test_late_invalidation_cannot_be_overwritten_by_stale_settlement_batch(tmp_path, monkeypatch):
    from guance.infrastructure.training_data.collector import TrainingDataCollector
    original = TrainingDataCollector.submit
    submitted, release = threading.Event(), threading.Event()

    def paused_after_settlement(self, message):
        batches = original(self, message)
        if message["message"] == "settlement":
            submitted.set()
            assert release.wait(5)
        return batches

    monkeypatch.setattr(TrainingDataCollector, "submit", paused_after_settlement)
    recorder = TrainingDataRecorder(tmp_path, source="synthetic", source_id="race")
    try:
        for update in sequence():
            recorder.observe_update(update)
        assert submitted.wait(2)
        recorder.invalidate_current("state_error")
        release.set()
        wait_for(lambda: recorder.stats()["written_episodes"] >= 1)
        recorder.close(1)
        final_records = records(tmp_path)
        assert len(final_records) == 1
        assert final_records[0]["status"] == "quarantine"
        assert final_records[0]["outcome"] is None
        assert manifest(tmp_path)["episodes"][0]["status"] == "quarantine"
        assert len(manifest(tmp_path)["episodes"]) == 1
    finally:
        release.set()
        recorder.close(1)
