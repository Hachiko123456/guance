"""DanKS V3 first-advice sample statistics - reference counting entry, v2.

This module is the phase 1 frozen reference for the unified D08 statistics
entry that the product must use verbatim in phase 5 (the production entry
records this version and any porting delta).  All counting rules live in
``compute_first_advice_metrics``:

- denominator: every pre-registered sample, deduplicated by sample_id (a
  repeated registration of the same sample_id never grows the denominator);
  the first registration owns the original monotonic ``t0`` and end reason,
  and attempts from all registrations of that sample_id are merged;
- a visible record is attributed only when ALL of the following hold:
  1. its full RequestIdentity matches a registered attempt of that sample,
     and the record's claimed sample_id equals the identity owner (claims
     never override the identity mapping);
  2. the owning attempt was actually queued (rejected or errored submissions
     never own visible records);
  3. the owning attempt is the sample's latest queued attempt (older
     generations are stale and never succeed);
  4. the visible time is not before the attempt's submission and not before
     the sample's original registration (renders cannot precede their own
     request or the registered confirmation);
- each sample succeeds at most once (deduplicated by sample_id); repeated
  renders and extra candidates never increase the numerator;
- every retry shares the sample's original t0/deadline; the deadline is
  evaluated against the original elapsed time, never against the last
  attempt's elapsed time;
- failures keep a classified reason: zero_submission, no_legal_visible,
  late_visible, timely; rejected visible records carry their own reason.

No latency is measured here; the function only evaluates registered inputs.
"""
from __future__ import annotations

from dataclasses import dataclass, field

STATISTICS_FUNCTION_VERSION = "danks-v3-sample-metrics-v2"

OUTCOME_ZERO_SUBMISSION = "zero_submission"
OUTCOME_NO_LEGAL_VISIBLE = "no_legal_visible"
OUTCOME_LATE_VISIBLE = "late_visible"
OUTCOME_TIMELY = "timely"

REJECT_UNREGISTERED_IDENTITY = "unregistered_identity"
REJECT_CLAIMED_SAMPLE_MISMATCH = "claimed_sample_mismatch"
REJECT_REJECTED_SUBMISSION = "rejected_submission_display"
REJECT_STALE_GENERATION = "stale_generation_display"
REJECT_BEFORE_SUBMISSION = "display_before_submission"
REJECT_BEFORE_REGISTRATION = "display_before_registration"


@dataclass(frozen=True)
class SampleAttempt:
    """One actual submission attempt of a registered sample.

    ``generation`` is derived from the attempt's full RequestIdentity tuple
    slot 5 (request_id, session_id, game_id, state_version, epoch,
    generation) so identity and generation cannot diverge.
    """

    request_identity: tuple  # full RequestIdentity tuple
    submitted_at: float
    outcome: str  # "queued" | "rejected_none" | "error"
    generation: int = 0

    def __post_init__(self) -> None:
        if len(self.request_identity) != 6:
            raise ValueError("request identity must be a 6-tuple")
        object.__setattr__(self, "generation", int(self.request_identity[5]))


@dataclass(frozen=True)
class RegisteredSample:
    """Pre-registered expected-recommendation sample (denominator unit)."""

    sample_id: str
    registered_t0: float
    state_tag: str
    model_tag: str
    attempts: tuple[SampleAttempt, ...] = ()


@dataclass(frozen=True)
class VisibleRecord:
    """One observed candidate render event for a produced request."""

    claimed_sample_id: str
    request_identity: tuple
    visible_at: float
    legal: bool


@dataclass(frozen=True)
class SampleOutcome:
    sample_id: str
    attempts: int
    queued_generations: tuple[int, ...]
    success: bool
    outcome: str
    original_elapsed: float | None
    last_attempt_elapsed: float | None
    merged_registrations: int = 1


@dataclass(frozen=True)
class MetricsResult:
    registered_samples: int
    registration_rows: int
    timely_samples: int
    ratio: float
    outcomes: tuple[SampleOutcome, ...]
    rejected_visible_records: int
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    function_version: str = STATISTICS_FUNCTION_VERSION


def _attempt_owner_map(samples: dict[str, RegisteredSample]) -> dict[tuple, tuple[str, SampleAttempt]]:
    owners: dict[tuple, tuple[str, SampleAttempt]] = {}
    for sample_id, sample in samples.items():
        for attempt in sample.attempts:
            owner = owners.get(attempt.request_identity)
            if owner is not None and owner[0] != sample_id:
                raise ValueError(
                    f"request identity registered for two samples: "
                    f"{attempt.request_identity}")
            owners[attempt.request_identity] = (sample_id, attempt)
    return owners


def compute_first_advice_metrics(samples: list[RegisteredSample],
                                 visible_records: list[VisibleRecord],
                                 budget_seconds: float) -> MetricsResult:
    if budget_seconds <= 0:
        raise ValueError("budget must be positive")

    # Deduplicate registrations by sample_id: the first registration owns the
    # original t0 and tags; attempts from every registration row are merged.
    registration_rows = len(samples)
    merged: dict[str, RegisteredSample] = {}
    merge_counts: dict[str, int] = {}
    for sample in samples:
        existing = merged.get(sample.sample_id)
        if existing is None:
            merged[sample.sample_id] = sample
            merge_counts[sample.sample_id] = 1
            continue
        merge_counts[sample.sample_id] += 1
        merged[sample.sample_id] = RegisteredSample(
            sample_id=sample.sample_id, registered_t0=existing.registered_t0,
            state_tag=existing.state_tag, model_tag=existing.model_tag,
            attempts=existing.attempts + sample.attempts)

    owners = _attempt_owner_map(merged)

    by_sample: dict[str, list[VisibleRecord]] = {
        sample_id: [] for sample_id in merged}
    rejected: dict[str, int] = {}
    for record in visible_records:
        owner = owners.get(record.request_identity)
        if owner is None:
            rejected[REJECT_UNREGISTERED_IDENTITY] = \
                rejected.get(REJECT_UNREGISTERED_IDENTITY, 0) + 1
            continue
        owner_id, attempt = owner
        if owner_id != record.claimed_sample_id:
            rejected[REJECT_CLAIMED_SAMPLE_MISMATCH] = \
                rejected.get(REJECT_CLAIMED_SAMPLE_MISMATCH, 0) + 1
            continue
        if record.visible_at < merged[owner_id].registered_t0:
            rejected[REJECT_BEFORE_REGISTRATION] = \
                rejected.get(REJECT_BEFORE_REGISTRATION, 0) + 1
            continue
        if attempt.outcome != "queued":
            rejected[REJECT_REJECTED_SUBMISSION] = \
                rejected.get(REJECT_REJECTED_SUBMISSION, 0) + 1
            continue
        if record.visible_at < attempt.submitted_at:
            rejected[REJECT_BEFORE_SUBMISSION] = \
                rejected.get(REJECT_BEFORE_SUBMISSION, 0) + 1
            continue
        sample = merged[owner_id]
        latest_queued = max((item for item in sample.attempts
                             if item.outcome == "queued"),
                            key=lambda item: (item.generation, item.submitted_at))
        if attempt.request_identity != latest_queued.request_identity:
            rejected[REJECT_STALE_GENERATION] = \
                rejected.get(REJECT_STALE_GENERATION, 0) + 1
            continue
        by_sample[owner_id].append(record)

    outcomes = []
    timely = 0
    for sample_id in sorted(merged):
        sample = merged[sample_id]
        records = by_sample[sample_id]
        success = False
        outcome = OUTCOME_NO_LEGAL_VISIBLE
        original_elapsed = None
        if not sample.attempts:
            outcome = OUTCOME_ZERO_SUBMISSION
        for record in records:
            elapsed = record.visible_at - sample.registered_t0
            if record.legal and elapsed <= budget_seconds:
                success = True
                outcome = OUTCOME_TIMELY
                original_elapsed = elapsed
                break
        if sample.attempts and not success and records:
            outcome = OUTCOME_LATE_VISIBLE
            original_elapsed = max(record.visible_at - sample.registered_t0
                                   for record in records)
        if sample.attempts and not success and not records:
            outcome = OUTCOME_NO_LEGAL_VISIBLE
        timely += 1 if success else 0
        last_elapsed = None
        if sample.attempts:
            last_visible = max((record.visible_at for record in records), default=None)
            if last_visible is not None:
                last_elapsed = last_visible - sample.attempts[-1].submitted_at
        outcomes.append(SampleOutcome(
            sample_id=sample_id, attempts=len(sample.attempts),
            queued_generations=tuple(sorted({item.generation for item in sample.attempts
                                             if item.outcome == "queued"})),
            success=success, outcome=outcome, original_elapsed=original_elapsed,
            last_attempt_elapsed=last_elapsed,
            merged_registrations=merge_counts[sample_id]))
    count = len(merged)
    return MetricsResult(
        registered_samples=count, registration_rows=registration_rows,
        timely_samples=timely, ratio=(timely / count) if count else 0.0,
        outcomes=tuple(outcomes),
        rejected_visible_records=sum(rejected.values()),
        rejection_reasons=rejected)
