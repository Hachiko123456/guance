"""Production first-advice sample statistics entry (D08).

Verbatim port of the phase 1 frozen reference counting rules
(``danks-v3-sample-metrics-v2``) into the application layer; the version tag
below is what acceptance tests and reports must record.  All counting rules
live in :func:`compute_first_advice_metrics`:

- denominator: every pre-registered sample, deduplicated by sample_id (a
  repeated registration never grows the denominator); the first registration
  owns the original monotonic ``t0``, and attempts from all registrations of
  that sample_id are merged;
- a visible record is attributed only when ALL of the following hold: its
  full RequestIdentity matches a registered attempt of that sample, the
  owning attempt was actually queued, the attempt is the sample's latest
  queued generation, and the visible time is not before the attempt's
  submission nor the sample's registration;
- displays of rejected submissions never succeed; each sample succeeds at
  most once; retries share the original t0/deadline;
- failures keep classified reasons; rejected visible records carry theirs.

``AdviceMetricsRecorder`` is the stateful entry the controller/service wire
into: registration at the upstream should-recommend confirmation, attempt
recording at the actual client submit, visible recording at the result
delivery boundary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic

from guance.contracts import RequestIdentity

STATISTICS_FUNCTION_VERSION = "danks-v3-sample-metrics-v4"

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

    ``generation`` derives from the attempt's full RequestIdentity tuple
    (request_id, session_id, game_id, state_version, epoch, generation).
    """

    request_identity: tuple
    submitted_at: float
    outcome: str  # "queued" | "rejected_none" | "error"
    generation: int = 0

    def __post_init__(self) -> None:
        if len(self.request_identity) != 6:
            raise ValueError("request identity must be a 6-tuple")
        object.__setattr__(self, "generation", int(self.request_identity[5]))


@dataclass(frozen=True)
class RegisteredSample:
    sample_id: str
    registered_t0: float
    state_tag: str
    model_tag: str
    budget_seconds: float | None = None  # None -> the global budget applies
    attempts: tuple[SampleAttempt, ...] = ()


@dataclass(frozen=True)
class VisibleRecord:
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


def compute_first_advice_metrics(samples: list[RegisteredSample],
                                 visible_records: list[VisibleRecord],
                                 budget_seconds: float) -> MetricsResult:
    """The single counting entry; see the module docstring for the rules."""
    if budget_seconds <= 0:
        raise ValueError("budget must be positive")

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
            budget_seconds=existing.budget_seconds,
            attempts=existing.attempts + sample.attempts)

    attempt_owner: dict[tuple, tuple[str, SampleAttempt]] = {}
    for sample_id, sample in merged.items():
        for attempt in sample.attempts:
            owner = attempt_owner.get(attempt.request_identity)
            if owner is not None and owner[0] != sample_id:
                raise ValueError(
                    f"request identity registered for two samples: "
                    f"{attempt.request_identity}")
            attempt_owner[attempt.request_identity] = (sample_id, attempt)

    by_sample: dict[str, list[VisibleRecord]] = {sample_id: [] for sample_id in merged}
    rejected: dict[str, int] = {}

    def reject(reason: str) -> None:
        rejected[reason] = rejected.get(reason, 0) + 1

    for record in visible_records:
        owner = attempt_owner.get(record.request_identity)
        if owner is None:
            reject(REJECT_UNREGISTERED_IDENTITY)
            continue
        owner_id, attempt = owner
        if owner_id != record.claimed_sample_id:
            reject(REJECT_CLAIMED_SAMPLE_MISMATCH)
            continue
        if record.visible_at < merged[owner_id].registered_t0:
            reject(REJECT_BEFORE_REGISTRATION)
            continue
        if attempt.outcome != "queued":
            reject(REJECT_REJECTED_SUBMISSION)
            continue
        if record.visible_at < attempt.submitted_at:
            reject(REJECT_BEFORE_SUBMISSION)
            continue
        sample = merged[owner_id]
        latest_queued = max((item for item in sample.attempts
                             if item.outcome == "queued"),
                            key=lambda item: (item.generation, item.submitted_at))
        if attempt.request_identity != latest_queued.request_identity:
            reject(REJECT_STALE_GENERATION)
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
        effective_budget = sample.budget_seconds if sample.budget_seconds is not None             else budget_seconds
        for record in records:
            elapsed = record.visible_at - sample.registered_t0
            if record.legal and elapsed <= effective_budget:
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
        outcomes=tuple(outcomes), rejected_visible_records=sum(rejected.values()),
        rejection_reasons=rejected)


def _identity_from_tuple(values: tuple) -> RequestIdentity:
    """Rebuild a RequestIdentity from a recorded 6-tuple."""
    return RequestIdentity(values[0], values[1], values[2], values[3],
                           values[4], values[5])


class AdviceMetricsRecorder:
    """Stateful production entry: registration, attempts, visible, aggregate.

    Attempts merge into the sample's existing registration (same sample_id);
    an attempt whose sample_id is unknown creates a new registration so a
    late/implicit sample still carries its failure evidence.
    """

    def __init__(self, *, budget_seconds: float, clock=monotonic) -> None:
        self._budget = float(budget_seconds)
        self._clock = clock
        self._registrations: dict[str, RegisteredSample] = {}
        self._order: list[str] = []
        self._visible: list[VisibleRecord] = []

    def register_sample(self, sample_id: str, *, state_tag: str = "",
                        model_tag: str = "", registered_t0: float | None = None,
                        budget_seconds: float | None = None) -> float:
        """Pre-register one should-recommend sample before any submit.

        The FIRST registration owns the original t0 and tags; repeats merge
        (denominator unchanged)."""
        t0 = self._clock() if registered_t0 is None else float(registered_t0)
        existing = self._registrations.get(sample_id)
        if existing is None:
            self._registrations[sample_id] = RegisteredSample(
                sample_id=sample_id, registered_t0=t0, state_tag=state_tag,
                model_tag=model_tag, budget_seconds=budget_seconds)
            self._order.append(sample_id)
        return existing.registered_t0 if existing is not None else t0

    def record_attempt(self, identity: RequestIdentity, *,
                       outcome: str = "queued",
                       submitted_at: float | None = None,
                       sample_id: str | None = None) -> None:
        """Record one actual submission attempt with its full wire identity."""
        key = (identity.request_id, identity.session_id, identity.game_id,
               identity.state_version, identity.epoch, identity.generation)
        attempt = SampleAttempt(request_identity=key,
                                submitted_at=self._clock() if submitted_at is None
                                else float(submitted_at),
                                outcome=outcome)
        if sample_id is None:
            sample_id = self._owner_of(key)
        if sample_id is None:
            sample_id = f"unattributed-{identity.request_id}"
        existing = self._registrations.get(sample_id)
        if existing is None:
            self._registrations[sample_id] = RegisteredSample(
                sample_id=sample_id, registered_t0=attempt.submitted_at,
                state_tag="", model_tag="", attempts=(attempt,))
            self._order.append(sample_id)
        else:
            self._registrations[sample_id] = RegisteredSample(
                sample_id=sample_id, registered_t0=existing.registered_t0,
                state_tag=existing.state_tag, model_tag=existing.model_tag,
                budget_seconds=existing.budget_seconds,
                attempts=existing.attempts + (attempt,))

    def record_visible(self, identity: RequestIdentity, *, legal: bool = True,
                       visible_at: float | None = None,
                       claimed_sample_id: str | None = None) -> None:
        """Record one rendered candidate at the delivery boundary."""
        key = (identity.request_id, identity.session_id, identity.game_id,
               identity.state_version, identity.epoch, identity.generation)
        self._visible.append(VisibleRecord(
            claimed_sample_id=claimed_sample_id if claimed_sample_id is not None
            else (self._owner_of(key) or ""),
            request_identity=key,
            visible_at=self._clock() if visible_at is None else float(visible_at),
            legal=legal))

    def _owner_of(self, key: tuple) -> str | None:
        for sample_id in reversed(self._order):
            for attempt in self._registrations[sample_id].attempts:
                if attempt.request_identity == key:
                    return sample_id
        return None

    def snapshot_metrics(self) -> MetricsResult:
        samples = [self._registrations[sample_id] for sample_id in self._order]
        return compute_first_advice_metrics(samples, list(self._visible),
                                            self._budget)
