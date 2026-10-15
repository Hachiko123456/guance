"""Qt-free own-view training capture; no traffic replay or action execution.

Live: TrainingDataRecorder(data_root), observe_update, invalidate_current on
errors/reconnect, then close(timeout=0.2) after existing stop cleanup. Construct
a new instance on each start. Advice is intentionally ignored. Offline:
TrainingDataCollector(source='legacy', source_id=source_sha256); persist batches
returned by observe_update and close. No legacy file parser is included here.
"""
from .collector import TrainingDataCollector
from .derive import derive_fabledan
from .legacy import convert_legacy, read_legacy_episodes
from .validation import validate_decision
from .recorder import TrainingDataRecorder
from .schema import SCHEMA_VERSION, SchemaError, action_signature, snapshot_update

__all__ = [
    "TrainingDataCollector", "TrainingDataRecorder", "SCHEMA_VERSION",
    "SchemaError", "action_signature", "snapshot_update", "validate_decision",
    "convert_legacy", "read_legacy_episodes", "derive_fabledan",
]
