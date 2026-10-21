"""Repair-1 counterexamples for the CPU/GPU numeric parity gate.

The gate must reject malformed, truncated, duplicated or numerically invalid
evidence.  Every case below is an independent negative counterexample; the
positive control proves the gate still accepts valid evidence.  It never relies
on ``zip`` truncation and it separates the 32 action/path-score comparisons from
the 4 probability/mask comparisons.
"""
from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_gpu_first_inference_acceptance.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("gpu_first_acceptance_runner", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


RUNNER = _load_runner()
VECTOR_LENGTH = 256
ACTION_COUNT = 32
NUMERIC_COUNT = 4


def _identity(index: int) -> str:
    return f"{index:064x}"


def _fixed_records():
    return {
        "requested_device": "cpu",
        "recommendation_count": ACTION_COUNT,
        "records": [
            {
                "decision_identity": _identity(index),
                "action_sha256": f"action-{index}",
                "path_score": -1.25 - index * 0.001,
                "kind": "single",
            }
            for index in range(ACTION_COUNT)
        ],
    }


def _probabilities(peak: float = 0.5, tail: float = 0.0) -> list[float]:
    values = [tail] * VECTOR_LENGTH
    values[0] = peak
    return values


def _numeric_records():
    return {
        "requested_device": "cpu",
        "records": [
            {
                "decision_identity": _identity(index),
                "probabilities": _probabilities(),
                "mask": [1] * VECTOR_LENGTH,
                "argmax": 0,
                "argmax_probability": 0.5,
                "sum": 0.5,
            }
            for index in range(NUMERIC_COUNT)
        ],
    }


def _write(path: Path, value) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _run(tmp_path: Path, cpu, gpu, numeric_cpu, numeric_gpu):
    paths = {
        "cpu": _write(tmp_path / "fixed-cpu.json", cpu),
        "gpu": _write(tmp_path / "fixed-gpu.json", gpu),
        "numeric_cpu": _write(tmp_path / "numeric-cpu.json", numeric_cpu),
        "numeric_gpu": _write(tmp_path / "numeric-gpu.json", numeric_gpu),
    }
    output = tmp_path / "parity.json"
    RUNNER.run_compare(paths["cpu"], paths["gpu"], paths["numeric_cpu"],
                       paths["numeric_gpu"], output)
    return json.loads(output.read_text(encoding="utf-8"))


def test_positive_control_reports_action_and_numeric_coverage_separately(tmp_path):
    summary = _run(tmp_path, _fixed_records(), _fixed_records(),
                   _numeric_records(), _numeric_records())
    assert summary["action_record_count"] == ACTION_COUNT
    assert summary["numeric_record_count"] == NUMERIC_COUNT
    assert summary["numeric_element_count"] == NUMERIC_COUNT * VECTOR_LENGTH
    assert summary["probability_within_tolerance"] is True
    assert summary["argmax_mismatches"] == 0
    assert summary["mask_mismatches"] == 0


def test_empty_action_records_are_rejected(tmp_path):
    cpu = _fixed_records()
    gpu = _fixed_records()
    cpu["records"] = []
    gpu["records"] = []
    with pytest.raises(RuntimeError, match="COMPARE_ACTION_EMPTY"):
        _run(tmp_path, cpu, gpu, _numeric_records(), _numeric_records())


def test_truncated_action_records_are_rejected(tmp_path):
    cpu = _fixed_records()
    gpu = _fixed_records()
    cpu["records"] = cpu["records"][:-1]
    with pytest.raises(RuntimeError, match="COMPARE_ACTION_COUNT"):
        _run(tmp_path, cpu, gpu, _numeric_records(), _numeric_records())


def test_duplicate_action_identity_is_rejected(tmp_path):
    cpu = _fixed_records()
    cpu["records"][-1]["decision_identity"] = cpu["records"][0]["decision_identity"]
    with pytest.raises(RuntimeError, match="COMPARE_DUPLICATE_IDENTITY"):
        _run(tmp_path, cpu, _fixed_records(), _numeric_records(), _numeric_records())


def test_missing_action_identity_is_rejected(tmp_path):
    gpu = _fixed_records()
    gpu["records"][-1]["decision_identity"] = _identity(999)
    with pytest.raises(RuntimeError, match="COMPARE_IDENTITY_SET_MISMATCH"):
        _run(tmp_path, _fixed_records(), gpu, _numeric_records(), _numeric_records())


def test_empty_numeric_records_are_rejected(tmp_path):
    with pytest.raises(RuntimeError, match="COMPARE_NUMERIC_EMPTY"):
        _run(tmp_path, _fixed_records(), _fixed_records(), _numeric_records(),
             {"requested_device": "cuda", "records": []})


def test_truncated_numeric_records_are_rejected(tmp_path):
    truncated = _numeric_records()
    truncated["records"] = truncated["records"][:-1]
    with pytest.raises(RuntimeError, match="COMPARE_NUMERIC_COUNT"):
        _run(tmp_path, _fixed_records(), _fixed_records(), _numeric_records(), truncated)


def test_duplicate_numeric_identity_is_rejected(tmp_path):
    cpu = _numeric_records()
    cpu["records"][-1]["decision_identity"] = cpu["records"][0]["decision_identity"]
    with pytest.raises(RuntimeError, match="COMPARE_DUPLICATE_IDENTITY"):
        _run(tmp_path, _fixed_records(), _fixed_records(), cpu, _numeric_records())


def test_numeric_identity_set_mismatch_is_rejected(tmp_path):
    gpu = _numeric_records()
    gpu["records"][-1]["decision_identity"] = _identity(999)
    with pytest.raises(RuntimeError, match="COMPARE_IDENTITY_SET_MISMATCH"):
        _run(tmp_path, _fixed_records(), _fixed_records(), _numeric_records(), gpu)


def test_probability_vector_length_mismatch_is_rejected(tmp_path):
    cpu = _numeric_records()
    cpu["records"][0]["probabilities"] = _probabilities()[:-1]
    with pytest.raises(RuntimeError, match="COMPARE_VECTOR_LENGTH"):
        _run(tmp_path, _fixed_records(), _fixed_records(), cpu, _numeric_records())


def test_mask_vector_length_mismatch_is_rejected(tmp_path):
    gpu = _numeric_records()
    gpu["records"][0]["mask"] = [1] * (VECTOR_LENGTH - 1)
    with pytest.raises(RuntimeError, match="COMPARE_VECTOR_LENGTH"):
        _run(tmp_path, _fixed_records(), _fixed_records(), _numeric_records(), gpu)


def test_nan_probability_is_rejected(tmp_path):
    cpu = _numeric_records()
    cpu["records"][0]["probabilities"][1] = float("nan")
    with pytest.raises(RuntimeError, match="COMPARE_NONFINITE"):
        _run(tmp_path, _fixed_records(), _fixed_records(), cpu, _numeric_records())


def test_inf_probability_is_rejected(tmp_path):
    gpu = _numeric_records()
    gpu["records"][0]["probabilities"][1] = float("inf")
    with pytest.raises(RuntimeError, match="COMPARE_NONFINITE"):
        _run(tmp_path, _fixed_records(), _fixed_records(), _numeric_records(), gpu)


def test_nan_path_score_is_rejected(tmp_path):
    cpu = _fixed_records()
    cpu["records"][0]["path_score"] = float("nan")
    with pytest.raises(RuntimeError, match="COMPARE_NONFINITE"):
        _run(tmp_path, cpu, _fixed_records(), _numeric_records(), _numeric_records())


def test_argmax_mismatch_is_a_failure(tmp_path):
    gpu = _numeric_records()
    gpu["records"][0]["argmax"] = 1
    with pytest.raises(RuntimeError, match="COMPARE_ARGMAX_MISMATCH"):
        _run(tmp_path, _fixed_records(), _fixed_records(), _numeric_records(), gpu)


def test_argmax_out_of_range_is_a_failure(tmp_path):
    gpu = _numeric_records()
    gpu["records"][0]["argmax"] = VECTOR_LENGTH
    with pytest.raises(RuntimeError, match="COMPARE_ARGMAX_INVALID"):
        _run(tmp_path, _fixed_records(), _fixed_records(), _numeric_records(), gpu)


def test_mask_mismatch_is_a_failure(tmp_path):
    gpu = _numeric_records()
    gpu["records"][0]["mask"][1] = 0
    with pytest.raises(RuntimeError, match="COMPARE_MASK_MISMATCH"):
        _run(tmp_path, _fixed_records(), _fixed_records(), _numeric_records(), gpu)


def test_small_probability_drift_above_elementwise_tolerance_is_rejected(tmp_path):
    cpu = _numeric_records()
    gpu = _numeric_records()
    cpu["records"][0]["probabilities"][1] = 0.0002
    gpu["records"][0]["probabilities"][1] = 0.00035
    # 1.5e-4 > atol 1e-4 + rtol 1e-3 * 3.5e-4; the old atol+rtol*1.0 rule
    # (1.1e-3) would have silently accepted this drift.
    with pytest.raises(RuntimeError, match="COMPARE_PROBABILITY_TOLERANCE"):
        _run(tmp_path, _fixed_records(), _fixed_records(), cpu, gpu)


def test_small_probability_drift_within_elementwise_tolerance_is_accepted(tmp_path):
    cpu = _numeric_records()
    gpu = _numeric_records()
    cpu["records"][0]["probabilities"][1] = 0.0002
    gpu["records"][0]["probabilities"][1] = 0.00029
    summary = _run(tmp_path, _fixed_records(), _fixed_records(), cpu, gpu)
    assert summary["max_probability_abs_difference"] == pytest.approx(9e-5)


def test_action_hash_difference_is_a_failure(tmp_path):
    gpu = _fixed_records()
    gpu["records"][0]["action_sha256"] = "different"
    with pytest.raises(RuntimeError, match="COMPARE_ACTION_DIFFERENCE"):
        _run(tmp_path, _fixed_records(), gpu, _numeric_records(), _numeric_records())


def test_path_score_difference_above_tolerance_is_a_failure(tmp_path):
    gpu = _fixed_records()
    gpu["records"][0]["path_score"] = gpu["records"][0]["path_score"] + 0.006
    with pytest.raises(RuntimeError, match="COMPARE_SCORE_TOLERANCE"):
        _run(tmp_path, _fixed_records(), gpu, _numeric_records(), _numeric_records())


def test_tolerance_is_not_widened_by_the_gate():
    tolerance = RUNNER.PREDECLARED_TOLERANCE
    assert tolerance["rtol"] == 1e-3
    assert tolerance["atol"] == 1e-4
    assert tolerance["score_abs"] == 5e-3
    # The old implementation compared against atol + rtol * 1.0 = 1.1e-3.
    assert tolerance["atol"] + tolerance["rtol"] * 1.0 > 1e-3
    assert math.isfinite(tolerance["atol"])
