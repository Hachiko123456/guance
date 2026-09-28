from __future__ import annotations

import json
from pathlib import Path

import pytest

from guance.contracts import GameObservation
from guance.infrastructure.model.advisor import FableDanAdvisor

ROOT = Path(__file__).parents[2]
MODEL = ROOT / "data" / "profiles" / "tencent_daguandan" / "models" / "best.npz"
FIXTURE = ROOT / "tests" / "fixtures" / "observations.json"


@pytest.fixture(scope="module")
def advisor() -> FableDanAdvisor:
    if not MODEL.is_file():
        pytest.skip("Private trained weights are not committed; provide local best.npz for model integration tests")
    instance = FableDanAdvisor(MODEL, top_n=3)
    metadata = instance.load()
    assert metadata["status"] == "loaded"
    return instance


def test_model_loads_required_numpy_profile(advisor: FableDanAdvisor) -> None:
    assert advisor.fingerprint
    assert advisor.metadata["backend"] == "numpy"
    assert advisor.metadata["config"]["vocab"] == 48
    assert advisor.metadata["config"]["feat_dim"] == 80


def test_real_observation_fixtures_produce_legal_advice(advisor: FableDanAdvisor) -> None:
    records = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert len(records) >= 3
    for record in records[:3] + records[-1:]:
        result = advisor.recommend(GameObservation.from_dict(record), request_id="test")
        assert result.status == "ready", result.error_message
        assert result.candidates
        if record["free_lead"]:
            assert result.candidates[0].cards
        assert set(result.candidates[0].cards).issubset(set(record["hand"]))


def test_invalid_state_fails_closed(advisor: FableDanAdvisor) -> None:
    record = json.loads(FIXTURE.read_text(encoding="utf-8"))[0]
    record["hand"] = []
    result = advisor.recommend(GameObservation.from_dict(record), request_id="invalid")
    assert result.status == "error"
    assert result.error_code == "MODEL_INPUT_OR_INFERENCE"
