"""History provenance gate for DanKS V3 eligibility (phase 3, D04).

The phase 1 investigation established that the state machine keeps
``public_history_origin`` and ``transport_completeness`` unsupported for
every input it can observe today, so real-capture provenance is explicitly
NOT certifiable yet.  This module makes the split explicit instead of
inferring it:

- ``HistoryProvenance.synthetic_normal_entry()``: inputs rebuilt through the
  normal SessionEngine entry (deal -> start -> seat confirmation -> events)
  are eligible for the offline core; their provenance basis is the
  by-construction replay recorded in the phase 1 fixtures.
- ``HistoryProvenance.from_public_state(state)``: facts projected from a
  GameState keep the state machine's own Knowledge statuses; while those are
  ``unsupported`` the provenance is ineligible (code
  ``HISTORY_PROVENANCE_UNVERIFIED``), which is what keeps D04/D10
  real-input gates BLOCKED until an upstream evidence source exists.

The gate never fabricates completeness: no local sequence numbers, remaining
counts or hand-written booleans can turn an unverified chain into provenance.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SYNTHETIC_NORMAL_ENTRY = "synthetic_normal_entry"
LIVE_CAPTURE = "live_capture"


@dataclass(frozen=True)
class HistoryProvenance:
    origin: str
    public_history_complete: str  # Knowledge status value
    transport_contiguous: str
    detail: str = ""

    @property
    def eligible(self) -> bool:
        if self.origin == SYNTHETIC_NORMAL_ENTRY:
            return True
        if self.origin == LIVE_CAPTURE:
            return (self.public_history_complete == "known"
                    and self.transport_contiguous == "known")
        return False

    @property
    def ineligible_reason(self) -> str:
        if self.eligible:
            return ""
        if self.origin == LIVE_CAPTURE:
            return (f"public_history_complete={self.public_history_complete!r}, "
                    f"transport_contiguous={self.transport_contiguous!r}")
        return f"unknown origin {self.origin!r}"

    def to_dict(self) -> dict[str, Any]:
        return {"origin": self.origin,
                "public_history_complete": self.public_history_complete,
                "transport_contiguous": self.transport_contiguous,
                "eligible": self.eligible,
                "ineligible_reason": self.ineligible_reason}


def synthetic_normal_entry() -> HistoryProvenance:
    """Provenance for chains rebuilt through the normal SessionEngine entry."""
    return HistoryProvenance(SYNTHETIC_NORMAL_ENTRY, "known", "known",
                             "replayed normal-entry chain (phase 1 fixtures)")


def live_capture() -> HistoryProvenance:
    """Provenance for states derived from real captures (unverified today)."""
    return HistoryProvenance(LIVE_CAPTURE, "unsupported", "unsupported",
                             "live capture without upstream continuity evidence")


def from_public_state(state, session_id: str) -> HistoryProvenance:
    """Project the provenance facts a GameState itself certifies.

    The state machine keeps origin/transport unsupported for everything it
    can observe today (phase 1 finding), so this projection is ineligible
    until the state machine gains a verified evidence source.
    """
    v2 = state.public_state_v2(session_id)
    history = v2.history
    return HistoryProvenance(
        LIVE_CAPTURE,
        history.public_history_complete.status.value,
        history.transport_contiguous.status.value,
        "projected from GameState.public_state_v2")
