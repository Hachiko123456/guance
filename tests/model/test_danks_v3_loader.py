"""D01 strict-loader tests for the DanKS V3 provider core.

Positive identity tests run against the real frozen checkpoint when it is
present on the host; every negative test builds a synthetic checkpoint from
a randomly initialized architecture so the suite is self-contained.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
import torch

from guance.infrastructure.model.danks_v3.loader import (
    DanksV3LoaderError, load_selector,
)
from guance.infrastructure.model.danks_v3 import verify_vendor_manifest
from guance.infrastructure.model.danks_v3.loader import PAYLOAD_KEYS
from guance.infrastructure.model.danks_v3.source_identity import (
    CHECKPOINT_SHA256, REFERENCE_COMMIT, VENDOR_MANIFEST_PATH,
)
from guance.infrastructure.model.danks_v3.vendor.danks.training import schema
from guance.infrastructure.model.danks_v3.vendor.danks.training.model import (
    EfficientTeamBeliefTop10Selector, selector_model_type,
)

CHECKPOINT_PATH = Path(os.environ.get(
    "GUANCE_DANKS_CHECKPOINT", r"E:\Downloads\top10_selector_v3_online.pt"))
_real_checkpoint = pytest.mark.skipif(
    not CHECKPOINT_PATH.is_file(), reason="frozen checkpoint not on this host")


def _base_payload() -> dict:
    # build_selector_from_checkpoint expects the state dict to already be in
    # the payload, so the synthetic checkpoint builds the architecture class
    # directly and serializes its freshly initialized weights.
    torch.manual_seed(20261120)
    model = EfficientTeamBeliefTop10Selector(
        schema.STATE_DIM, schema.CANDIDATE_DIM,
        hidden_dim=256, candidate_hidden_dim=192)
    for parameter in model.parameters():
        parameter.data.normal_(0.0, 0.01)
    return {
        "kind": "top10_online",
        "model_type": schema.FEATURE_VERSION,
        "feature_version": schema.FEATURE_VERSION,
        "state_dim": schema.STATE_DIM,
        "candidate_dim": schema.CANDIDATE_DIM,
        "history_length": schema.HISTORY_LENGTH,
        "history_event_dim": schema.HISTORY_EVENT_DIM,
        "history_protocol": schema.HISTORY_PROTOCOL,
        "history_event_semantics": schema.HISTORY_EVENT_SEMANTICS,
        "action_support": schema.STRUCTURED_TOPK,
        "candidate_capacity": schema.TOPK,
        "team_belief_protocol": schema.TEAM_BELIEF_PROTOCOL,
        "model_config": {"hidden_dim": 256, "candidate_hidden_dim": 192},
        "model_state_dict": model.state_dict(),
    }


def _save_checkpoint(payload: dict, tmp_path: Path) -> tuple[Path, str]:
    target = tmp_path / "synthetic.pt"
    torch.save(payload, target)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    return target, digest


def test_vendor_manifest_pins_the_frozen_reference_commit():
    counts = verify_vendor_manifest()
    assert counts["files"] == counts["manifest_entries"] == 23
    manifest = json.loads(VENDOR_MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest["reference_commit"] == REFERENCE_COMMIT
    assert REFERENCE_COMMIT == "c618f537c7a83c77b76c3082985e42efff67ec02"
    for entry in manifest["files"]:
        assert len(entry["original_sha256"]) == 64
        assert entry["vendored_path"].startswith("src/guance/")


def test_synthetic_checkpoint_loads_strictly(tmp_path: Path):
    target, digest = _save_checkpoint(_base_payload(), tmp_path)
    loaded = load_selector(target, expected_sha256=digest)
    assert loaded.model_type == schema.FEATURE_VERSION
    assert loaded.state_dim == schema.STATE_DIM
    assert loaded.candidate_dim == schema.CANDIDATE_DIM
    assert (loaded.history_length, loaded.history_event_dim) == \
        (schema.HISTORY_LENGTH, schema.HISTORY_EVENT_DIM)
    assert loaded.action_support == schema.STRUCTURED_TOPK
    assert loaded.candidate_capacity == schema.TOPK
    assert not any(parameter.requires_grad for parameter in loaded.model.parameters())


def test_missing_checkpoint_file_fails_closed(tmp_path: Path):
    with pytest.raises(DanksV3LoaderError) as excinfo:
        load_selector(tmp_path / "absent.pt")
    assert excinfo.value.code == "MODEL_SOURCE_INVALID"


def test_checkpoint_identity_mismatch_rejected(tmp_path: Path):
    target, _digest = _save_checkpoint(_base_payload(), tmp_path)
    wrong = "0" * 64
    with pytest.raises(DanksV3LoaderError) as excinfo:
        load_selector(target, expected_sha256=wrong)
    assert excinfo.value.code == "MODEL_SOURCE_IDENTITY_MISMATCH"


def test_payload_key_mutation_rejected(tmp_path: Path):
    payload = _base_payload()
    payload.pop("team_belief_protocol")
    target, digest = _save_checkpoint(payload, tmp_path)
    with pytest.raises(DanksV3LoaderError) as excinfo:
        load_selector(target, expected_sha256=digest)
    assert excinfo.value.code == "MODEL_CHECKPOINT_INVALID"


def test_feature_version_mismatch_rejected(tmp_path: Path):
    payload = _base_payload()
    payload["feature_version"] = "top10_selector_v99_rogue"
    target, digest = _save_checkpoint(payload, tmp_path)
    with pytest.raises(DanksV3LoaderError) as excinfo:
        load_selector(target, expected_sha256=digest)
    assert excinfo.value.code == "MODEL_VERSION_UNSUPPORTED"


def test_dimension_mismatch_rejected(tmp_path: Path):
    payload = _base_payload()
    payload["state_dim"] = schema.STATE_DIM + 1
    target, digest = _save_checkpoint(payload, tmp_path)
    with pytest.raises(DanksV3LoaderError) as excinfo:
        load_selector(target, expected_sha256=digest)
    assert excinfo.value.code == "MODEL_VERSION_UNSUPPORTED"


def test_non_finite_weights_rejected(tmp_path: Path):
    payload = _base_payload()
    name = next(iter(payload["model_state_dict"]))
    payload["model_state_dict"][name] = torch.full_like(
        payload["model_state_dict"][name], float("nan"))
    target, digest = _save_checkpoint(payload, tmp_path)
    with pytest.raises(DanksV3LoaderError) as excinfo:
        load_selector(target, expected_sha256=digest)
    assert excinfo.value.code == "MODEL_WEIGHTS_INVALID"


def test_missing_state_dict_key_rejected(tmp_path: Path):
    payload = _base_payload()
    state_dict = dict(payload["model_state_dict"])
    state_dict.pop(next(iter(state_dict)))
    payload["model_state_dict"] = state_dict
    target, digest = _save_checkpoint(payload, tmp_path)
    with pytest.raises(DanksV3LoaderError) as excinfo:
        load_selector(target, expected_sha256=digest)
    # Rejected either at the architecture build's strict-compatibility check
    # or at the loader's strict load; both enforce zero missing keys.
    assert excinfo.value.code in {"MODEL_WEIGHTS_INVALID", "MODEL_CHECKPOINT_INVALID"}


def test_unexpected_state_dict_key_rejected(tmp_path: Path):
    payload = _base_payload()
    state_dict = dict(payload["model_state_dict"])
    first = next(iter(state_dict))
    state_dict["rogue.extra.weight"] = state_dict[first].clone()
    payload["model_state_dict"] = state_dict
    target, digest = _save_checkpoint(payload, tmp_path)
    with pytest.raises(DanksV3LoaderError) as excinfo:
        load_selector(target, expected_sha256=digest)
    assert excinfo.value.code in {"MODEL_WEIGHTS_INVALID", "MODEL_CHECKPOINT_INVALID"}


def test_payload_keys_match_the_frozen_schema_set():
    assert len(PAYLOAD_KEYS) == 14


@_real_checkpoint
def test_real_checkpoint_loads_with_frozen_identity():
    loaded = load_selector(CHECKPOINT_PATH)
    assert loaded.checkpoint_sha256 == CHECKPOINT_SHA256
    assert loaded.model_type == schema.FEATURE_VERSION
    assert selector_model_type(loaded.model) == loaded.model_type


@_real_checkpoint
def test_real_checkpoint_rejects_alternate_expected_identity():
    with pytest.raises(DanksV3LoaderError) as excinfo:
        load_selector(CHECKPOINT_PATH, expected_sha256="1" * 64)
    assert excinfo.value.code == "MODEL_SOURCE_IDENTITY_MISMATCH"
