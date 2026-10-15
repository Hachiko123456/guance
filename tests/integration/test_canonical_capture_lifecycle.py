from __future__ import annotations

import json

from guance.infrastructure.capture.canonical_capture import CanonicalCaptureConverter
from tests.local_capture.canonical_capture_fixture import (
    create_capture, decision_input, finish_capture, record_confirmed,
)


def test_lifecycle_order_and_repeated_close_are_idempotent(tmp_path):
    recorder, converter, source, capture_id = create_capture(tmp_path)
    record_confirmed(converter, decision_input(capture_id))
    finish_capture(recorder)
    first = converter.close()
    journal = (source.parent / "phase7_canonical_decisions.jsonl").read_bytes()
    manifest = (source.parent / "phase7_acceptance_input.json").read_bytes()
    second = converter.close()
    assert first == second
    assert (source.parent / "phase7_canonical_decisions.jsonl").read_bytes() == journal
    assert (source.parent / "phase7_acceptance_input.json").read_bytes() == manifest
    assert not (source.parent / "phase7_canonical_decisions.jsonl.tmp").exists()
    assert not (source.parent / "phase7_acceptance_input.json.tmp").exists()


def test_premature_producer_close_is_blocked_not_positive(tmp_path):
    recorder, converter, source, capture_id = create_capture(tmp_path)
    record_confirmed(converter, decision_input(capture_id))
    result = converter.close()
    assert result.status == "blocked"
    assert not (source.parent / "phase7_acceptance_input.json").exists()
    status = json.loads((source.parent / "phase7_acceptance_status.json").read_text(encoding="utf-8"))
    assert status["status"] == "blocked"
    recorder.close()


def test_journal_close_failure_cannot_write_eligible_manifest(tmp_path):
    recorder, converter, source, capture_id = create_capture(tmp_path)
    record_confirmed(converter, decision_input(capture_id))
    finish_capture(recorder)
    underlying = converter._stream

    class FailingJournal:
        def flush(self):
            raise OSError("synthetic journal failure")

        def fileno(self):
            return underlying.fileno()

        def close(self):
            underlying.close()

    converter._stream = FailingJournal()
    result = converter.close()
    assert result.status == "blocked"
    assert "JOURNAL_CLOSE_FAILED" in result.missing_fact_codes
    assert underlying.closed
    assert not (source.parent / "phase7_acceptance_input.json").exists()
    assert not (source.parent / "phase7_canonical_decisions.jsonl").exists()


def test_crash_temporary_file_is_never_positive_input(tmp_path):
    recorder, _converter, source, capture_id = create_capture(tmp_path)
    stale = source.parent / "phase7_canonical_decisions.jsonl.tmp"
    # Close the first producer's private stream before simulating a crashed stale file.
    _converter._stream.close(); _converter._stream = None
    stale.write_text("stale", encoding="utf-8")
    converter = CanonicalCaptureConverter(source.parent, capture_id)
    assert stale.exists() and stale.read_text(encoding="utf-8") == ""
    record_confirmed(converter, decision_input(capture_id))
    finish_capture(recorder)
    assert converter.close().status == "eligible"

def test_source_close_failure_cannot_write_eligible_input(tmp_path):
    recorder, converter, source, capture_id = create_capture(tmp_path)
    record_confirmed(converter, decision_input(capture_id))
    underlying = recorder._stream

    class FailingSource:
        def flush(self):
            raise OSError("synthetic source close failure")

        def close(self):
            underlying.close()

    recorder._stream = FailingSource()
    recorder.close()
    assert underlying.closed
    result = converter.close()
    assert result.status == "blocked"
    assert not (source.parent / "phase7_acceptance_input.json").exists()
    sidecar = json.loads((source.parent / "capture_status.json").read_text(encoding="utf-8"))
    assert sidecar["state"] == "close_failed"
