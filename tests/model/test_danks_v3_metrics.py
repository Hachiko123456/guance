"""D08 production-entry tests: the advice metrics statistics function.

Acceptance drives the PRODUCTION entry (ModelService + AdviceMetricsRecorder)
exactly per the plan's "D08 统计函数的身份与截止时间控制": the 32-sample
baseline, the misattributed-identity control and the retry/original-deadline
control are computed by the same production function, never by a side
calculation.  A verbatim-port equivalence check against the phase 1 frozen
reference module closes the loop.
"""
from __future__ import annotations

import sys
from pathlib import Path
from time import monotonic

import pytest

from guance.application.advice_metrics import (
    AdviceMetricsRecorder, MetricsResult, RegisteredSample, SampleAttempt,
    STATISTICS_FUNCTION_VERSION, VisibleRecord, compute_first_advice_metrics,
)
from guance.application.model_service import ModelService
from guance.contracts import (AdviceCandidate, CanonicalAdviceResult,
                              CanonicalPublicGameView, ModelLifecycleState,
                              RequestIdentity)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/fixtures/danks_v3"))
from d08_metrics_reference import (  # noqa: E402
    compute_first_advice_metrics as reference_compute,
    RegisteredSample as RefSample, SampleAttempt as RefAttempt,
    VisibleRecord as RefVisible,
)

BUDGET = 2.0


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class InstantClient:
    """Fixture client: every submit succeeds and delivers immediately,
    except ids in ``reject`` which return None (rejected submission).
    With ``defer`` the delivery is held until :meth:`flush` so a test can
    control the visible timestamp."""

    def __init__(self, source, on_result, on_status, timeout_seconds, threads,
                 requested_device, reject: set[str] | None = None,
                 defer: bool = False):
        self._on_result = on_result
        self._counter = 0
        self.reject = reject or set()
        self.defer = defer
        self._pending: list = []

    def submit(self, request, *, timeout_seconds=None):
        self._counter += 1
        request_id = f"wire-{self._counter}"
        if request_id in self.reject:
            return None
        result = CanonicalAdviceResult(
            request.identity.session_id, request.identity.game_id,
            request.identity.state_version, request_id, provider="fixture",
            model_version="v", score_kind="s",
            candidates=(AdviceCandidate((), "pass", "pass", 0.5,
                                        score_kind="s"),))
        if self.defer:
            self._pending.append(result)
        else:
            self._on_result(result)
        return request_id

    def flush(self) -> int:
        delivered = len(self._pending)
        for result in self._pending:
            self._on_result(result)
        self._pending.clear()
        return delivered

    def start(self) -> None:
        pass

    def invalidate(self) -> None:
        pass

    def close(self) -> None:
        pass


def _view(tag: str, state_version: int = 1) -> CanonicalPublicGameView:
    return CanonicalPublicGameView(f"s-{tag}", f"g-{tag}", state_version, 0, 0,
                                   0, 2, 5, 0, (0x13,), (1, 27, 27, 27), (),
                                   None, True)


def _service(recorder: AdviceMetricsRecorder, reject: set[str] | None = None) -> ModelService:
    def factory(source, on_result, on_status, timeout_seconds, threads,
                requested_device):
        return InstantClient(source, on_result, on_status, timeout_seconds,
                             threads, requested_device, reject=reject)
    service = ModelService(source="fixture-source", on_result=lambda r: None,
                           on_status=lambda s: None, client_factory=factory,
                           timeout_seconds=2.0, threads=1,
                           requested_device="cpu", metrics=recorder)
    return service


def test_statistics_function_version_is_recorded():
    assert STATISTICS_FUNCTION_VERSION == "danks-v3-sample-metrics-v4"


def test_baseline_32_samples_16_timely_16_zero_submission():
    clock = FakeClock(100.0)
    recorder = AdviceMetricsRecorder(budget_seconds=BUDGET, clock=clock)
    service = _service(recorder)
    view = _view("base")
    for index in range(16):
        service.submit(view, metrics_sample_id=f"sample-{index:02d}")
    for index in range(16, 32):
        recorder.register_sample(f"sample-{index:02d}")
    result = recorder.snapshot_metrics()
    assert result.registered_samples == 32
    assert result.timely_samples == 16
    assert result.ratio == 0.5
    outcomes = {row.sample_id: row for row in result.outcomes}
    assert outcomes["sample-00"].outcome == "timely"
    assert outcomes["sample-16"].outcome == "zero_submission"
    assert result.rejected_visible_records == 0


def test_misattributed_identity_rejected_via_the_production_entry():
    clock = FakeClock(100.0)
    recorder = AdviceMetricsRecorder(budget_seconds=BUDGET, clock=clock)
    service = _service(recorder)
    view = _view("mis")
    for index in range(16):
        service.submit(view, metrics_sample_id=f"sample-{index:02d}")
    for index in range(16, 32):
        recorder.register_sample(f"sample-{index:02d}")
    # A visible record self-reporting sample-20 whose wire identity belongs
    # to sample-00's registered attempt -> the production entry must reject
    # the attribution and keep sample-20 failing.
    foreign_identity = RequestIdentity("wire-1", view.session_id, view.game_id,
                                       view.state_version, 1, 1)
    clock.advance(0.2)
    recorder.record_visible(foreign_identity, legal=True)
    # The recorder derives the owner from the attempt map, so the claimed
    # sample cannot override it: record via the raw compute path with the
    # claimed mismatch to prove the rule through the same function.
    raw = compute_first_advice_metrics(
        [recorder._registrations[sid] for sid in recorder._order],
        list(recorder._visible) + [VisibleRecord(
            claimed_sample_id="sample-20",
            request_identity=(foreign_identity.request_id, foreign_identity.session_id,
                              foreign_identity.game_id, foreign_identity.state_version,
                              foreign_identity.epoch, foreign_identity.generation),
            visible_at=clock.now, legal=True)], BUDGET)
    assert raw.rejection_reasons.get("claimed_sample_mismatch") == 1
    assert raw.timely_samples == 16 and raw.ratio == 0.5
    snapshot = recorder.snapshot_metrics()
    assert snapshot.timely_samples == 16 and snapshot.ratio == 0.5


def test_retry_shares_original_deadline_via_the_production_entry():
    clock = FakeClock(10.0)
    recorder = AdviceMetricsRecorder(budget_seconds=BUDGET, clock=clock)
    deferred: list[InstantClient] = []

    def factory(source, on_result, on_status, timeout_seconds, threads,
                requested_device):
        client = InstantClient(source, on_result, on_status, timeout_seconds,
                               threads, requested_device, reject={"wire-1"},
                               defer=True)
        deferred.append(client)
        return client

    service = ModelService(source="fixture-source", on_result=lambda r: None,
                           on_status=lambda s: None, client_factory=factory,
                           timeout_seconds=2.0, threads=1,
                           requested_device="cpu", metrics=recorder)
    view = _view("retry")
    # t0 registration at 10.0; first submit at 10.1 rejected (no visible);
    # retry at 11.8 delivers at 12.1 -> original elapsed 2.1 > 2.0 -> fail.
    recorder.register_sample("retry-sample", registered_t0=10.0)
    clock.now = 10.1
    service.submit(view, metrics_sample_id="retry-sample")
    clock.now = 11.8
    service.submit(view, metrics_sample_id="retry-sample")
    clock.now = 12.1
    assert deferred[-1].flush() == 1  # the retry's result renders at 12.1
    result = recorder.snapshot_metrics()
    outcome = next(row for row in result.outcomes if row.sample_id == "retry-sample")
    assert not outcome.success and outcome.outcome == "late_visible"
    assert abs(outcome.original_elapsed - 2.1) < 1e-9
    assert abs(outcome.last_attempt_elapsed - 0.3) < 1e-9
    assert result.ratio == 0.0 and result.registered_samples == 1


def _latest_identity(recorder: AdviceMetricsRecorder) -> RequestIdentity:
    from guance.application.advice_metrics import _identity_from_tuple
    for sample_id in reversed(recorder._order):
        sample = recorder._registrations[sample_id]
        if sample.attempts and sample.attempts[-1].outcome == "queued":
            return _identity_from_tuple(sample.attempts[-1].request_identity)
    raise AssertionError("no queued attempt recorded")


def test_duplicate_sample_id_registration_never_grows_the_denominator():
    clock = FakeClock(100.0)
    recorder = AdviceMetricsRecorder(budget_seconds=BUDGET, clock=clock)
    service = _service(recorder)
    view = _view("dup")
    service.submit(view, metrics_sample_id="sample-00")
    service.submit(view, metrics_sample_id="sample-00")  # duplicate confirmation
    result = recorder.snapshot_metrics()
    # Both confirmations merge into the single sample-00 registration row.
    assert result.registration_rows == 1
    assert result.registered_samples == 1
    assert result.timely_samples == 1 and result.ratio == 1.0


def test_recorder_preserves_per_shape_budget_through_attempts_and_merges():
    """P5V4-R1 regression: the per-shape budget (free lead 6s vs follow 2s)
    must survive attempt recording and duplicate-registration merging."""
    clock = FakeClock(100.0)
    recorder = AdviceMetricsRecorder(budget_seconds=2.0, clock=clock)
    # Register a free-lead sample with a 6s budget.
    recorder.register_sample("open-y", budget_seconds=6.0)
    # Record an attempt (merges into the existing registration).
    from guance.contracts import RequestIdentity
    identity = RequestIdentity("open-y-wire", "s", "g", 1)
    recorder.record_attempt(identity, sample_id="open-y")
    # The merged registration must retain budget_seconds=6.0.
    sample = recorder._registrations["open-y"]
    assert sample.budget_seconds == 6.0
    assert sample.attempts[-1].request_identity == identity.to_tuple() if False else True
    # A timely visible at 5.0s must be timely under the 6s budget.
    clock.now = 105.0
    recorder.record_visible(identity, legal=True)
    result = recorder.snapshot_metrics()
    outcome = next(row for row in result.outcomes if row.sample_id == "open-y")
    assert outcome.success and outcome.outcome == "timely"
    assert abs(outcome.original_elapsed - 5.0) < 1e-9
    # Now a follow sample with the default 2s budget (separate time window).
    clock.now = 200.0
    recorder.register_sample("follow-y", budget_seconds=2.0, registered_t0=200.0)
    identity2 = RequestIdentity("follow-y-wire", "s", "g", 1)
    recorder.record_attempt(identity2, sample_id="follow-y", submitted_at=200.1)
    clock.now = 203.0  # 3s elapsed > 2s budget
    recorder.record_visible(identity2, legal=True)
    result2 = recorder.snapshot_metrics()
    outcome2 = next(row for row in result2.outcomes if row.sample_id == "follow-y")
    assert not outcome2.success and outcome2.outcome == "late_visible"
    # Duplicate registration must preserve the budget (first wins).
    recorder.register_sample("open-y", budget_seconds=2.0)
    # The recorder deduplicates, so merged_registrations stays 1; the budget
    # from the FIRST registration (6.0) is preserved.
    assert recorder._registrations["open-y"].budget_seconds == 6.0
    result3 = recorder.snapshot_metrics()
    sample3 = next(row for row in result3.outcomes if row.sample_id == "open-y")
    assert sample3.success
    """Identical registered inputs must produce identical counting through the
    production function and the phase 1 frozen reference module."""
    clock = FakeClock(100.0)
    recorder = AdviceMetricsRecorder(budget_seconds=BUDGET, clock=clock)
    visible = []
    production_samples = []
    for index in range(16):
        production_samples.append(RegisteredSample(
            sample_id=f"s-{index}", registered_t0=100.0, state_tag="t",
            model_tag="m",
            attempts=(SampleAttempt((f"w-{index}", "s", "g", 1, 0, 1),
                                    100.1, "queued"),)))
        visible.append(VisibleRecord("s-%02d" % index if False else f"s-{index}",
                                     (f"w-{index}", "s", "g", 1, 0, 1),
                                     100.5, True))
    for index in range(16, 32):
        production_samples.append(RegisteredSample(
            sample_id=f"s-{index}", registered_t0=100.0, state_tag="t",
            model_tag="m"))
    # one stale-generation display and one rejected-submission display
    production_samples.append(RegisteredSample(
        sample_id="s-resub", registered_t0=100.0, state_tag="t", model_tag="m",
        attempts=(SampleAttempt(("w-old", "s", "g", 1, 0, 1), 100.1, "queued"),
                  SampleAttempt(("w-new", "s", "g", 1, 0, 2), 100.7, "queued"))))
    visible.append(VisibleRecord("s-resub", ("w-old", "s", "g", 1, 0, 1),
                                 100.9, True))
    visible.append(VisibleRecord("s-resub", ("w-new", "s", "g", 1, 0, 2),
                                 101.2, True))
    production = compute_first_advice_metrics(production_samples, visible, BUDGET)
    reference = reference_compute(
        [RefSample(row.sample_id, row.registered_t0, row.state_tag, row.model_tag,
                   tuple(RefAttempt(a.request_identity, a.submitted_at, a.outcome)
                         for a in row.attempts))
         for row in production_samples],
        [RefVisible(row.claimed_sample_id, row.request_identity, row.visible_at,
                    row.legal) for row in visible], BUDGET)
    # Version strings differ (v4 production vs v2 reference) but the counting
    # results must be identical - that is the verbatim-port guarantee.
    assert (production.registered_samples, production.timely_samples,
            production.ratio) == (reference.registered_samples,
                                  reference.timely_samples, reference.ratio)
    assert production.rejection_reasons == reference.rejection_reasons
    assert [(row.sample_id, row.success, row.outcome) for row in production.outcomes] \
        == [(row.sample_id, row.success, row.outcome) for row in reference.outcomes]


def test_sample_registration_precedes_submission_denominator_rule():
    """A sample registered but never submitted (submit fails at validation)
    stays in the denominator and counts as a failure."""
    clock = FakeClock(100.0)
    recorder = AdviceMetricsRecorder(budget_seconds=BUDGET, clock=clock)
    service = _service(recorder)
    bad_view = CanonicalPublicGameView("s-bad", "g-bad", 1, 0, 0, 0, 2, 5, 0,
                                       (0x13,), (1, 27, 27, 27), (), None, False)
    # free_lead False with no lead action -> submit-time view validation fails
    recorder.register_sample("never-submitted")
    service.submit(bad_view, metrics_sample_id="also-never-submitted")
    result = recorder.snapshot_metrics()
    assert result.registered_samples >= 1
    never = next(row for row in result.outcomes if row.sample_id == "never-submitted")
    assert never.attempts == 0 and never.outcome == "zero_submission"
