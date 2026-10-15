from __future__ import annotations

from dataclasses import replace
import json
from threading import Event, Thread

import pytest

from guance.infrastructure.model.marvel_v2 import (
    AbstractCacheLifecycle, CacheLifecycleError, CacheReceipt,
    ExactHistoryJournal, HistoryWorkspace, MarvelEvent,
)

ARTIFACT_SHA = "a" * 64
NEW_ARTIFACT_SHA = "b" * 64


def _journal(*, state_id="state-1") -> ExactHistoryJournal:
    return ExactHistoryJournal.from_events(
        session_id="session-1", game_id="game-1", state_id=state_id,
        artifact_id="iter000100", artifact_sha256=ARTIFACT_SHA,
        events=(
            MarvelEvent("RESET", "LEAD_PLAY", 0, level=1),
            MarvelEvent("PRIVATE_PREFIX", "LEAD_PLAY", 0, tokens=(8,)),
            MarvelEvent("PRIVATE_PREFIX", "LEAD_PLAY", 0, tokens=(18,)),
            MarvelEvent("PRIVATE_PREFIX", "LEAD_PLAY", 0, tokens=(39,)),
            MarvelEvent("PRIVATE_PREFIX", "LEAD_PLAY", 0, tokens=(64,)),
            MarvelEvent("PUBLIC_COMMIT", "LEAD_PLAY", 0,
                        tokens=(8, 18, 39, 64, 7)),
        ),
    )


def _lifecycle(**kwargs) -> AbstractCacheLifecycle:
    return AbstractCacheLifecycle(
        lifecycle_id="lifecycle-1", session_id="session-1", game_id="game-1",
        artifact_id="iter000100",
        artifact_sha256=ARTIFACT_SHA, **kwargs,
    )


def test_lifecycle_identity_is_required_and_bounded():
    with pytest.raises(TypeError):
        AbstractCacheLifecycle(
            session_id="session-1", game_id="game-1", artifact_id="iter000100",
            artifact_sha256=ARTIFACT_SHA,
        )
    with pytest.raises(CacheLifecycleError, match="cache_lifecycle_id_invalid"):
        AbstractCacheLifecycle(
            lifecycle_id="x" * 129, session_id="session-1", game_id="game-1",
            artifact_id="iter000100", artifact_sha256=ARTIFACT_SHA,
        )


def test_from_history_builds_initial_receipt_without_model_runtime():
    journal = _journal()
    lifecycle = AbstractCacheLifecycle.from_history(
        journal, lifecycle_id="lifecycle-factory", session_id="session-1",
        game_id="game-1", artifact_id="iter000100", artifact_sha256=ARTIFACT_SHA,
    )
    receipt = lifecycle.receipt_for(journal)
    assert receipt.history == journal.identity
    assert receipt.generation == 0
    assert lifecycle.status == "active"


def test_sync_callback_is_registered_before_builder_and_receipt_is_available():
    journal = _journal()
    lifecycle = _lifecycle()
    observations = []

    def builder(request, history):
        observations.append((lifecycle.is_pending(request), lifecycle.pending_count,
                             request.generation, history.history_sha256))
        return CacheReceipt.rebuild(request, history)

    receipt = lifecycle.run_rebuild(journal, request_key="decision", builder=builder)
    assert observations == [(True, 1, 0, journal.history_sha256)]
    assert lifecycle.pending_count == 0
    assert lifecycle.receipt_for(journal) == receipt
    compare_request = lifecycle.begin_request(journal, request_key="compare")
    compare_receipt = lifecycle.build_receipt(compare_request, journal)
    assert receipt.content_sha256 == compare_receipt.content_sha256
    lifecycle.complete(compare_request, compare_receipt)


def test_request_identity_is_foreign_across_lifecycle_instances():
    journal = _journal()
    lifecycle_a = _lifecycle()
    request_a = lifecycle_a.begin_request(journal, request_key="decision")
    receipt_a = lifecycle_a.build_receipt(request_a, journal)
    lifecycle_b = AbstractCacheLifecycle(
        lifecycle_id="lifecycle-2", session_id="session-1", game_id="game-1",
        artifact_id="iter000100", artifact_sha256=ARTIFACT_SHA,
    )
    request_b = lifecycle_b.begin_request(journal, request_key="decision")
    assert request_a.request_id != request_b.request_id
    with pytest.raises(CacheLifecycleError, match="cache_result_stale_or_foreign"):
        lifecycle_b.complete(request_a, receipt_a)


def test_same_key_a_b_supersedes_a_even_for_same_history():
    journal = _journal()
    lifecycle = _lifecycle()
    request_a = lifecycle.begin_request(journal, request_key="decision")
    receipt_a = lifecycle.build_receipt(request_a, journal)
    request_b = lifecycle.begin_request(journal, request_key="decision")
    receipt_b = lifecycle.build_receipt(request_b, journal)
    assert request_a.request_id != request_b.request_id
    with pytest.raises(CacheLifecycleError, match="cache_result_stale_or_foreign"):
        lifecycle.complete(request_a, receipt_a)
    assert lifecycle.complete(request_b, receipt_b) == receipt_b


def test_concurrent_same_key_b_wins_and_late_a_is_rejected():
    journal = _journal()
    lifecycle = _lifecycle()
    a_ready = Event()
    release_a = Event()
    result: list[str] = []

    def late_a() -> None:
        request = lifecycle.begin_request(journal, request_key="decision")
        receipt = lifecycle.build_receipt(request, journal)
        a_ready.set()
        assert release_a.wait(timeout=5)
        try:
            lifecycle.complete(request, receipt)
        except CacheLifecycleError as exc:
            result.append(exc.code)

    thread = Thread(target=late_a)
    thread.start()
    assert a_ready.wait(timeout=5)
    request_b = lifecycle.begin_request(journal, request_key="decision")
    receipt_b = lifecycle.build_receipt(request_b, journal)
    lifecycle.complete(request_b, receipt_b)
    release_a.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert result == ["cache_result_stale_or_foreign"]
    assert lifecycle.receipt_for(journal) == receipt_b


def test_foreign_or_mutated_result_identity_is_rejected():
    journal = _journal()
    lifecycle = _lifecycle()
    request = lifecycle.begin_request(journal, request_key="decision")
    receipt = lifecycle.build_receipt(request, journal)
    forged = replace(receipt, request_id="f" * 64)
    with pytest.raises(CacheLifecycleError):
        lifecycle.complete(request, forged)
    assert lifecycle.is_pending(request)
    forged_content = replace(receipt, content_sha256="e" * 64)
    with pytest.raises(CacheLifecycleError, match="cache_receipt_integrity_invalid"):
        lifecycle.complete(request, forged_content)
    assert lifecycle.is_pending(request)
    assert lifecycle.complete(request, receipt) == receipt
    with pytest.raises(CacheLifecycleError, match="cache_result_stale_or_foreign"):
        lifecycle.complete(request, receipt)


@pytest.mark.parametrize("transition", ["invalidate", "restart", "crash", "close"])
def test_transitions_reject_late_callbacks(transition):
    journal = _journal()
    lifecycle = _lifecycle()
    request = lifecycle.begin_request(journal, request_key="decision")
    receipt = lifecycle.build_receipt(request, journal)
    getattr(lifecycle, transition)()
    assert lifecycle.generation == 1
    with pytest.raises(CacheLifecycleError):
        lifecycle.complete(request, receipt)
    assert lifecycle.pending_count == 0
    assert lifecycle.receipt_count == 0


def test_timeout_rejects_late_callback_and_advances_generation():
    journal = _journal()
    lifecycle = _lifecycle()
    request = lifecycle.begin_request(journal, request_key="decision")
    receipt = lifecycle.build_receipt(request, journal)
    lifecycle.timeout(request)
    assert lifecycle.generation == 1
    with pytest.raises(CacheLifecycleError):
        lifecycle.complete(request, receipt)
    with pytest.raises(CacheLifecycleError, match="cache_timeout_request_stale"):
        lifecycle.timeout(request)


def test_reload_rejects_old_artifact_and_accepts_exact_new_identity():
    journal = _journal()
    lifecycle = _lifecycle()
    request = lifecycle.begin_request(journal, request_key="decision")
    receipt = lifecycle.build_receipt(request, journal)
    lifecycle.reload(artifact_id="iter000200", artifact_sha256=NEW_ARTIFACT_SHA)
    with pytest.raises(CacheLifecycleError):
        lifecycle.complete(request, receipt)
    with pytest.raises(CacheLifecycleError, match="cache_history_identity_mismatch"):
        lifecycle.begin_request(journal, request_key="decision")
    new_journal = replace(journal, artifact_id="iter000200",
                          artifact_sha256=NEW_ARTIFACT_SHA)
    rebuilt = lifecycle.run_rebuild(new_journal, request_key="decision")
    assert rebuilt.generation == 1


def test_crash_requires_restart_and_close_is_terminal():
    journal = _journal()
    lifecycle = _lifecycle()
    lifecycle.crash()
    with pytest.raises(CacheLifecycleError, match="cache_lifecycle_crashed"):
        lifecycle.begin_request(journal, request_key="decision")
    lifecycle.restart()
    assert lifecycle.run_rebuild(journal, request_key="decision").generation == 2
    lifecycle.close()
    with pytest.raises(CacheLifecycleError, match="cache_lifecycle_closed"):
        lifecycle.restart()
    with pytest.raises(CacheLifecycleError, match="cache_lifecycle_closed"):
        lifecycle.begin_request(journal, request_key="decision")


def test_restart_rebuild_preserves_content_history_counters_and_cursor_only():
    journal = _journal(state_id="state-final")
    lifecycle = _lifecycle()
    first = lifecycle.run_rebuild(journal, request_key="decision")
    generation_zero_receipt = first.receipt_sha256
    lifecycle.restart()
    second = lifecycle.run_rebuild(journal, request_key="decision")
    assert second.generation == 1
    assert second.content_sha256 == first.content_sha256
    assert second.receipt_sha256 != generation_zero_receipt
    assert second.history.history_sha256 == first.history.history_sha256
    assert second.history.cursor == first.history.cursor == journal.cursor
    assert second.history.token_step == 5
    assert second.history.committed_step == 1


def test_speculative_receipt_is_separate_and_never_replaces_confirmed():
    journal = _journal()
    lifecycle = _lifecycle()
    confirmed = lifecycle.run_rebuild(journal, request_key="confirmed")
    workspace = HistoryWorkspace(journal).clone_branch("branch-a")
    branch = workspace.branch("branch-a")
    branch_receipt = lifecycle.run_rebuild(branch, request_key="speculative")
    assert branch_receipt.history.branch_id == "branch-a"
    assert branch_receipt.history.cache_key != confirmed.history.cache_key
    assert lifecycle.receipt_for(journal) == confirmed
    assert lifecycle.receipt_for(branch) == branch_receipt
    assert journal.identity == confirmed.history


def test_receipt_and_pending_bounds_fail_without_eviction_or_truncation():
    journal = _journal()
    branch = HistoryWorkspace(journal).clone_branch("branch-a").branch("branch-a")
    lifecycle = _lifecycle(max_receipts=1)
    confirmed = lifecycle.run_rebuild(journal, request_key="confirmed")
    request = lifecycle.begin_request(branch, request_key="speculative")
    receipt = lifecycle.build_receipt(request, branch)
    with pytest.raises(CacheLifecycleError, match="cache_receipt_overflow"):
        lifecycle.complete(request, receipt)
    assert lifecycle.receipt_for(journal) == confirmed
    pending = _lifecycle(max_pending=1)
    pending.begin_request(journal, request_key="a")
    with pytest.raises(CacheLifecycleError, match="cache_pending_overflow"):
        pending.begin_request(journal, request_key="b")
    assert pending.pending_count == 1


def test_builder_failure_removes_request_without_creating_receipt():
    lifecycle = _lifecycle()
    with pytest.raises(CacheLifecycleError, match="cache_receipt_builder_failed"):
        lifecycle.run_rebuild(
            _journal(), request_key="decision",
            builder=lambda _request, _history: (_ for _ in ()).throw(
                RuntimeError("synthetic failure")
            ),
        )
    assert lifecycle.pending_count == 0
    assert lifecycle.receipt_count == 0
    assert lifecycle.last_diagnostic.code == "request_builder_failed"


def test_diagnostics_are_bounded_metadata_only_and_do_not_leak_payloads():
    journal = _journal()
    lifecycle = _lifecycle()
    lifecycle.run_rebuild(journal, request_key="decision")
    payload = lifecycle.last_diagnostic.as_dict()
    assert set(payload) == {
        "code", "operation", "status", "generation", "lifecycle_sha256",
        "request_sha256",
        "history_sha256", "branch_sha256", "event_count", "token_step",
        "committed_step", "pending_count", "receipt_count",
    }
    serialized = json.dumps(payload, sort_keys=True).lower()
    for forbidden in (
        "event_tokens", "observer_streams", "physical_cards", "own_hand",
        "private_prefix", "tensor", "probability", "authorization", "cookie",
    ):
        assert forbidden not in serialized
    assert "session-1" not in serialized
    assert "game-1" not in serialized
    assert len(payload["lifecycle_sha256"]) == 64
    assert len(payload["request_sha256"]) == 64
    assert payload["event_count"] == 6
    assert payload["token_step"] == 5
    assert payload["committed_step"] == 1
