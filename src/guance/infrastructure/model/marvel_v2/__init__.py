"""Lazy MARVEL V2 namespace.

The default registry imports only ``marvel_v2.identity``.  Keeping this package
initializer lazy ensures FableDan/default startup does not import the Phase 3
encoder/action stack, Torch, or a native extension.
"""
from __future__ import annotations

from importlib import import_module

_EXPORTS = {
    "ACTION_ROW_WIDTH": "contracts", "CHANNEL_COUNT": "contracts",
    "CHANNEL_SLICES": "contracts",
    "EVENT_WIDTH": "contracts", "MARVEL_PROTOCOL": "contracts",
    "MAX_ACTION_ROWS": "contracts", "OBSERVER_COUNT": "contracts",
    "ActionDeclaration": "contracts", "ActionProtocolError": "contracts",
    "ActionRow": "contracts", "CardMapping": "contracts", "CommitIdentity": "contracts",
    "DecodedAction": "contracts", "ExchangeFacts": "contracts",
    "ExchangeTransferFacts": "contracts", "LegalActionRows": "contracts",
    "MarvelEvent": "contracts", "MarvelProtocolError": "contracts",
    "MarvelState": "contracts", "PreviousResultFacts": "contracts",
    "UnsupportedStateError": "contracts",
    "ActionCandidate": "action_decoder", "DecodedExchangeResolution": "action_decoder",
    "declaration_tokens": "action_decoder", "decode_canonical_tokens": "action_decoder",
    "decode_committed_tokens": "action_decoder", "decode_exchange_resolution": "action_decoder",
    "ActionProtocolSession": "action_protocol", "ProtocolStep": "action_protocol",
    "generate_legal_rows": "action_protocol",
    "adapt_live_state": "encoder", "encode_channels": "encoder",
    "encode_channels_batch": "encoder", "encode_observer_channels": "encoder",
    "encode_event": "event_encoder", "encode_history": "event_encoder",
    "AbstractCacheLifecycle": "cache_lifecycle",
    "ExactHistoryJournal": "history", "ExactObserverHistory": "history",
    "HistoryStep": "history", "HistoryWorkspace": "history",
    "SpeculativeHistoryBranch": "history", "SpeculativeBranch": "history",
    "MAX_BRANCHES": "lifecycle_contracts",
    "MAX_HISTORY_STEPS": "lifecycle_contracts",
    "MAX_PENDING_REQUESTS": "lifecycle_contracts",
    "MAX_RECEIPTS": "lifecycle_contracts",
    "CacheLifecycleError": "lifecycle_contracts",
    "CacheReceipt": "lifecycle_contracts",
    "CacheRequestIdentity": "lifecycle_contracts",
    "HistoryCursor": "lifecycle_contracts",
    "HistoryIdentity": "lifecycle_contracts",
    "LifecycleDiagnostic": "lifecycle_contracts",
    "ModelArtifactIdentity": "lifecycle_contracts",
    "SessionIdentity": "lifecycle_contracts",
    "CacheLifecycle": "cache_lifecycle",
    "AbstractCacheReceipt": "cache_lifecycle",
    "CacheRequest": "cache_lifecycle",
    "DeploymentIdentity": "deployment",
    "collect_deployment_identity": "deployment",
    "require_platform_distinct_from_training": "deployment",
    "NumericParity": "parity", "compare_numeric": "parity",
    "compare_probabilities": "parity",
}

__all__ = tuple(_EXPORTS)


def __getattr__(name: str):
    try:
        module_name = _EXPORTS[name]
    except KeyError:
        raise AttributeError(name) from None
    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value
    return value
