"""Validated public+self decision adapter for the experimental live subset."""
from __future__ import annotations

from dataclasses import dataclass

from guance.infrastructure.capture.canonical_capture import ConfirmedDecisionInput
from .contracts import LegalActionRows
from .encoder import encode_channels


class LiveDecisionAdapterError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class AdaptedLiveDecision:
    observer_seat: int
    current_observer_channels: tuple[int, ...]
    observer_history: tuple[tuple[int, ...], ...]
    legal_rows: LegalActionRows
    history_identity: str
    cache_request_id: str


class MarvelV2ConfirmedDecisionAdapter:
    """Prove tensor-contract eligibility without loading weights or inventing peers."""

    identity = "MarvelV2ConfirmedDecisionAdapter.v1"

    def adapt(self, value: ConfirmedDecisionInput) -> AdaptedLiveDecision:
        if type(value) is not ConfirmedDecisionInput:
            raise LiveDecisionAdapterError("CONFIRMED_DECISION_REQUIRED")
        try:
            value.public_state.validate()
            value.marvel_state.validate()
            history = value.history.identity
            value.cache_request.validate()
            if value.cache_request.history != history:
                raise LiveDecisionAdapterError("CACHE_REQUEST_HISTORY_MISMATCH")
            rows = value.action_session.legal_rows
            rows.validate()
            observer = value.marvel_state.observer_seat
            stream = value.history.observer_streams[observer]
            channels = encode_channels(value.marvel_state)
            if (value.action_session.state_version != value.public_state.identity.state_version
                    or value.action_session.token_step != history.token_step
                    or value.action_session.committed_step != history.committed_step):
                raise LiveDecisionAdapterError("DECISION_COUNTER_MISMATCH")
            return AdaptedLiveDecision(
                observer, channels, stream, rows, history.cache_key,
                value.cache_request.request_id,
            )
        except LiveDecisionAdapterError:
            raise
        except Exception:
            raise LiveDecisionAdapterError("LIVE_MODEL_INPUT_UNAVAILABLE") from None


__all__ = [
    "AdaptedLiveDecision", "LiveDecisionAdapterError",
    "MarvelV2ConfirmedDecisionAdapter",
]
