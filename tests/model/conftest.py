"""Synthetic, temporary model fixtures only; no capture or training-data inputs."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from guance.contracts import CanonicalPublicGameView, GameObservation
from guance.infrastructure.model.manifest import legacy_npz_bundle


@pytest.fixture
def observation() -> GameObservation:
    return GameObservation("test-session", "test-game", 7, 123.0, 2, 0, 0, 0,
                           (0x11, 0x13), (2, 27, 27, 27), free_lead=True)


@pytest.fixture
def view(observation: GameObservation) -> CanonicalPublicGameView:
    return CanonicalPublicGameView.from_observation(observation)


@pytest.fixture
def npz_path(tmp_path: Path) -> Path:
    # Tiny zero-block synthetic FableDanNet: exercises the real NumPy path without a checkpoint.
    path = tmp_path / "synthetic.npz"
    np.savez(path,
        __config__=np.array(["n_blocks=0", "feat_dim=80", "vocab=48"]),
        rope_cos=np.zeros((1, 1), dtype=np.float32),
        rope_sin=np.zeros((1, 1), dtype=np.float32),
        **{"token_emb.weight": np.zeros((48, 2), dtype=np.float32),
           "final_norm.weight": np.ones(2, dtype=np.float32),
           "hand_mlp.0.weight": np.zeros((2, 80), dtype=np.float32),
           "hand_mlp.0.bias": np.zeros(2, dtype=np.float32),
           "q_head.0.weight": np.zeros((1, 4), dtype=np.float32),
           "q_head.0.bias": np.array([0.25], dtype=np.float32)})
    return path


@pytest.fixture
def manifest_data(npz_path: Path) -> dict:
    return legacy_npz_bundle(npz_path).manifest.to_dict()


@pytest.fixture
def manifest_path(npz_path: Path, manifest_data: dict) -> Path:
    path = npz_path.parent / "manifest.json"
    path.write_text(json.dumps(manifest_data, sort_keys=True), encoding="utf-8")
    return path
