"""Safe local-capture selector and evidence-contract checks; no capture read."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
MANIFEST_PATH = ROOT / "tests" / "local_capture_manifest.json"
SCRIPT_PATH = ROOT / "scripts" / "run_local_capture_acceptance.py"


def _manifest() -> list[dict[str, object]]:
    value = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, list)
    return value


def _script_module():
    spec = importlib.util.spec_from_file_location("phase7_local_capture_acceptance", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_local_capture_manifest_has_exact_selectors_and_safe_metadata_only():
    entries = _manifest()
    assert len(entries) == 5
    assert [entry["capture_id"] for entry in entries] == [
        "20260929-162605", "20260929-173126", "20260929-232820",
        "20260930-012733", "20260930-171722",
    ]
    for entry in entries:
        relative = Path(str(entry["relative_path"]))
        assert relative.parts == ("debug", "captures", entry["capture_id"], "frames.jsonl")
        assert not relative.is_absolute()
        assert ".." not in relative.parts
        digest = str(entry["source_sha256"])
        assert len(digest) == hashlib.sha256().digest_size * 2
        assert all(char in "0123456789abcdef" for char in digest)
        assert 0 <= int(entry["expected_observer_seat"]) <= 3
        assert int(entry["expected_decision_count"]) > 0
        assert set(entry) <= {
            "fixture_id", "capture_id", "relative_path", "source_sha256",
            "expected_level", "expected_observer_seat", "expected_decision_count",
            "expected_quality", "status",
        }
        assert "closed" in str(entry["expected_quality"])


def test_local_capture_selector_is_explicit_and_missing_sources_cannot_skip():
    test_source = (ROOT / "tests" / "local_capture" / "test_acceptance.py").read_text(encoding="utf-8")
    script_source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "@pytest.mark.local_capture" in test_source
    assert "pytest.skip" not in test_source
    assert "required=True" in script_source
    assert "not path.is_file() or not path.is_relative_to(root)" in script_source
    assert 'before_hash != entry["source_sha256"]' in script_source
    assert "before_hash != after_hash" in script_source
    assert "before_dir != after_dir" in script_source


def test_local_capture_output_guard_rejects_content_and_paths():
    module = _script_module()
    safe = {"capture_id": "20260930-012733", "source_sha256": "0" * 64,
            "status": "blocked", "unsupported_codes": {"GUANDAN_STATE_UNAVAILABLE": 1},
            "coverage": {"seats": [0], "levels": [8], "pass": 1}}
    module.validate_safe_output(safe)
    for unsafe in (
        {"cards": [1]},
        {"status": "D:\\project\\private"},
        {"hidden_opponent_hands": []},
        {"status": "line1\nline2"},
    ):
        with pytest.raises(ValueError, match="^LOCAL_CAPTURE_PRIVACY_CONTRACT_INVALID$"):
            module.validate_safe_output(unsafe)


def test_decision_probe_allows_public_self_fields_but_rejects_future_fields():
    module = _script_module()
    from guance.contracts import GameObservation

    observation = GameObservation(
        "synthetic-session", "synthetic-game", 7, 1.0, 8, 0, 0, 0,
        (17,), (1, 27, 27, 27), free_lead=True,
    )
    record = {
        "status": "accepted_candidate",
        "privacy_scope": "own_hand_and_public_actions_only",
        "quality": {"status": "accepted_candidate", "lost": False},
        "pre_state": observation.to_dict(),
        "observed_action": {"index": 1, "kind": "pass", "cards": []},
    }
    entry = {"capture_id": "20260930-012733", "expected_observer_seat": 0,
             "expected_level": 8}
    accepted = module._decision_summary(entry, [record], None)
    assert accepted["canonical_views"] == 1
    assert accepted["unsupported_decisions"] == 1
    assert accepted["unsupported_codes"] == {"MARVEL_COUNTERS_UNAVAILABLE": 1}
    rejected_record = json.loads(json.dumps(record))
    rejected_record["pre_state"]["reward"] = 1
    rejected = module._decision_summary(entry, [rejected_record], None)
    assert rejected["canonical_views"] == 0
    assert rejected["unsupported_codes"] == {"MODEL_INPUT_INVALID": 1}

    class UnexpectedAdvisor:
        def recommend(self, _view, _request_id):
            return object()

    unexpected = module._decision_summary(entry, [record], UnexpectedAdvisor())
    assert unexpected["model_inferences"] == 1
    assert unexpected["invalid_recommendations"] == 1
    assert unexpected["supported_decisions"] == 0
    assert unexpected["unsupported_codes"]["MODEL_OUTPUT_UNVERIFIED"] == 1


def test_local_capture_script_never_prints_content_bearing_objects():
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "print(record" not in source
    assert "print(view" not in source
    assert "print(episodes" not in source
    assert "print(json.dumps(result" in source
