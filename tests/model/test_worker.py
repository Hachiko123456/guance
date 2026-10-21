"""MBA-P0-001/P0-002/P0-004/P0-006 and MBA-P1-005: generic fail-closed JSONL."""
from dataclasses import replace
import io
import json

import pytest

from guance.contracts import AdviceCandidate, CanonicalAdviceResult, WORKER_PROTOCOL_VERSION
from guance.infrastructure.model.worker import MAX_LINE, dumps, envelope, run

IDS = {"request_id": "request-1", "session_id": "test-session", "game_id": "test-game",
       "state_version": 7, "epoch": 2, "generation": 11}


def execute(messages, **kwargs):
    output = io.StringIO()
    code = run(input_stream=io.StringIO("".join(messages)), output_stream=output, **kwargs)
    return code, [json.loads(line) for line in output.getvalue().splitlines()]


def test_generic_load_recommend_stop_preserves_all_ids(manifest_path, view):
    request = envelope("recommend", IDS, request_kind="public", observation=view.to_dict())
    code, replies = execute([dumps(envelope("load", IDS, source=str(manifest_path))),
                            dumps(request), dumps(envelope("stop", IDS))])
    assert code == 0
    assert [r["type"] for r in replies] == ["status", "loaded", "result", "status"]
    for reply in replies:
        assert {key: reply[key] for key in IDS} == IDS
        assert reply["protocol_version"] == WORKER_PROTOCOL_VERSION
    result_ids = {"request_id", "session_id", "game_id", "state_version"}
    assert {key: replies[2]["result"][key] for key in result_ids} == {
        key: IDS[key] for key in result_ids}
    assert replies[2]["result"]["score_kind"] == "fabledan_q"
    assert replies[-1]["model_status"] == "stopped"


@pytest.mark.parametrize("raw", ["not-json\n", "[]\n", "null\n", "1\n", "{}\n",
    '{"type":"stop","type":"recommend"}\n',
    json.dumps({**envelope("stop", IDS),
                "protocol_version": WORKER_PROTOCOL_VERSION + 1}) + "\n",
    json.dumps(envelope("stop", IDS)), "x" * (MAX_LINE + 1) + "\n",
    '"' + "界" * (MAX_LINE // 2) + '"\n'], ids=["invalid-json", "array", "null", "number", "no-version",
    "duplicate", "unsupported-version", "unterminated", "oversized-ascii", "oversized-utf8"])
def test_malformed_nonobject_oversized_or_unsupported_is_fatal(raw):
    code, replies = execute([raw, dumps(envelope("stop", IDS))])
    assert code == 3
    assert len(replies) == 1
    assert replies[0]["type"] == "error"
    assert replies[0]["fatal"] is True
    assert replies[0]["error_code"] in ("PROTOCOL_INVALID", "PROTOCOL_TOO_LARGE", "PROTOCOL_UNSUPPORTED")


@pytest.mark.parametrize("change", ["state", "hidden", "unknown"])
def test_mismatched_state_and_unexpected_fields_terminate(view, change):
    item = envelope("recommend", IDS, request_kind="public", observation=view.to_dict())
    if change == "state":
        item["state_version"] += 1
    elif change == "hidden":
        item["observation"]["opponent_hands"] = "SECRET_SENTINEL"
    else:
        item["type"] = "arbitrary"
    code, replies = execute([dumps(item)])
    assert code == 3
    assert replies[0]["fatal"] is True
    assert "SECRET_SENTINEL" not in json.dumps(replies)
    assert {key: replies[0][key] for key in IDS} == {key: item[key] for key in IDS}


def test_failed_explicit_load_does_not_reuse_previous_provider(manifest_path, view):
    code, replies = execute([
        dumps(envelope("load", IDS, source=str(manifest_path))),
        dumps(envelope("load", IDS, source=str(manifest_path.parent / "missing.npz"))),
        dumps(envelope("recommend", IDS, request_kind="public", observation=view.to_dict())),
    ])
    assert code == 0
    assert [r["error_code"] for r in replies if r["type"] == "error"] == ["ARTIFACT_MISSING", "MODEL_NOT_LOADED"]
    assert not any(r["type"] == "result" for r in replies)


def test_provider_exceptions_and_metadata_never_expose_internals(view, capsys):
    class Advisor:
        def load(self):
            return {"provider": "synthetic", "payload": "SECRET_SENTINEL",
                    "model_version": {"authorization": "SECRET_SENTINEL"}}
        def recommend(self, view, request_id):
            print("SECRET_SENTINEL")
            raise RuntimeError("cookie=SECRET_SENTINEL")
        def close(self):
            pass
    class Factory:
        def create(self, source):
            return Advisor()
    code, replies = execute([dumps(envelope("load", IDS, source="synthetic")),
        dumps(envelope("recommend", IDS, request_kind="public", observation=view.to_dict()))], factory=Factory())
    assert code == 0
    assert replies[1]["metadata"] == {"provider": "synthetic"}
    assert replies[-1]["error_code"] == "MODEL_INFERENCE_FAILED"
    assert "SECRET_SENTINEL" not in json.dumps(replies)
    assert not capsys.readouterr().out


@pytest.mark.parametrize("mode", ["mismatch", "not_owned", "empty_free_lead"])
def test_provider_output_identity_and_ownership_validated(view, mode):
    class Advisor:
        def load(self):
            return {}
        def recommend(self, view, request_id):
            cards = () if mode == "empty_free_lead" else (0x22,) if mode == "not_owned" else (0x11,)
            result = CanonicalAdviceResult(*view.key, request_id,
                provider="test", model_version="v1", score_kind="test_score",
                candidates=(AdviceCandidate(cards, "play", "single", 0.5, score_kind="test_score"),))
            return replace(result, state_version=999) if mode == "mismatch" else result
        def close(self):
            pass
    class Factory:
        def create(self, source):
            return Advisor()
    code, replies = execute([dumps(envelope("load", IDS, source="synthetic")),
        dumps(envelope("recommend", IDS, request_kind="public", observation=view.to_dict()))], factory=Factory())
    assert code == 0
    assert replies[-1]["type"] == "error"
    assert not any(r["type"] == "result" for r in replies)


def test_legacy_worker_cli_source_remains_supported(npz_path, view):
    code, replies = execute([dumps(envelope("recommend", IDS, request_kind="public", observation=view.to_dict()))],
                            weights_path=str(npz_path))
    assert code == 0
    assert replies[1]["type"] == "loaded"
    assert replies[-1]["type"] == "result"


def test_deterministic_json():
    assert dumps({"z": 1, "a": 2}) == dumps({"a": 2, "z": 1}) == '{"a":2,"z":1}\n'


def test_unknown_identity_field_is_rejected_fail_closed():
    message = envelope("stop", IDS)
    message["request_epoch"] = 3
    code, replies = execute([dumps(message)])
    assert code == 3
    assert replies[0]["type"] == "error"
    assert replies[0]["fatal"] is True
    assert replies[0]["error_code"] == "PROTOCOL_INVALID"


def test_duplicate_identity_field_is_rejected_fail_closed():
    raw = dumps(envelope("stop", IDS)).rstrip("\n")
    raw = raw.replace('"request_id":"request-1"',
                      '"request_id":"request-1","request_id":"request-2"') + "\n"
    code, replies = execute([raw])
    assert code == 3
    assert replies[0]["type"] == "error"
    assert replies[0]["fatal"] is True
    assert replies[0]["error_code"] == "PROTOCOL_INVALID"


def test_unsupported_protocol_keeps_valid_request_identity():
    message = envelope("stop", IDS)
    message["protocol_version"] = WORKER_PROTOCOL_VERSION + 1
    code, replies = execute([json.dumps(message) + "\n"])
    assert code == 3
    assert {key: replies[0][key] for key in IDS} == IDS
    assert replies[0]["error_code"] == "PROTOCOL_UNSUPPORTED"


def test_guandan_worker_load_cannot_report_ready(manifest_path):
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data.update(provider="builtin.guandan", backend="guandan", runtime="torch",
                artifact_format="torch_checkpoint", input_contract="guandan-public-v1",
                rules_version="GD-RULES-0.1", action_version="GD-ACTION-0.1",
                environment_version="GD-ENV-0.1", encoding_version="GD-ENCODING-0.1")
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    code, replies = execute([dumps(envelope("load", IDS, source=str(manifest_path)))])
    assert code == 0
    assert not any(item["type"] == "loaded" for item in replies)
    assert replies[-1]["error_code"] == "MODEL_UNAVAILABLE"


def test_phase6_reload_and_close_replace_exactly_one_provider_session():
    created = []
    class Advisor:
        def __init__(self, source):
            self.source = source
            self.closed = 0
        def load(self):
            return {"status": "loaded", "provider": "test", "backend": "test",
                    "runtime": "test", "model_version": self.source,
                    "model_sha256": "a" * 64,
                    "capabilities": {"recommend": True}}
        def recommend(self, view, request_id):
            raise AssertionError("recommend not expected")
        def close(self):
            self.closed += 1
    class Factory:
        def create(self, source):
            advisor = Advisor(source)
            created.append(advisor)
            return advisor
    code, replies = execute([
        dumps(envelope("load", IDS, source="first")),
        dumps(envelope("reload", IDS, source="second")),
        dumps(envelope("close", IDS)),
    ], factory=Factory())
    assert code == 0
    assert [item["type"] for item in replies] == ["status", "loaded", "status", "loaded", "status"]
    assert [item.get("model_status") for item in replies if item["type"] == "status"] == [
        "loading", "loading", "closed"]
    assert len(created) == 2
    assert created[0].source == "first" and created[0].closed == 1
    assert created[1].source == "second" and created[1].closed == 1
