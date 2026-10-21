"""MBA-P0-004, MBA-P1-005: latest-only queue, identities, timeout/restart/close."""
from dataclasses import replace
import threading
import time
from types import SimpleNamespace

import pytest

from guance.contracts import (AdviceCandidate, CanonicalAdviceResult, PublicRecommendationRequest,
                               RequestIdentity)
from guance.infrastructure.model.client import InferenceClient
from guance.infrastructure.model.errors import ModelBackendError
from guance.infrastructure.model.worker import envelope


def _submit(client, view, **kwargs):
    return client.submit(PublicRecommendationRequest(
        RequestIdentity("", view.session_id, view.game_id, view.state_version), view), **kwargs)


def wait_until(predicate, timeout=4):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate(), "Timed out waiting for model test event"


def reply(item):
    result = CanonicalAdviceResult(item["session_id"], item["game_id"], item["state_version"],
        item["request_id"], provider="test", model_version="v1", score_kind="test_score",
        candidates=(AdviceCandidate((0x11,), "play", "single", 0.5, score_kind="test_score"),))
    return envelope("result", item, result=result.to_dict())


class ControlledClient(InferenceClient):
    def __init__(self, tmp_path, results, statuses):
        super().__init__(tmp_path / "synthetic.npz", on_result=results.append,
                         on_status=statuses.append, timeout_seconds=0.3)
        self.sent = []
        self.launches = 0
        self.terminations = 0
    def _launch(self):
        self.launches += 1
        self._process = SimpleNamespace(poll=lambda: None)
        return True
    def _send(self, item):
        self.sent.append(item)
    def _terminate(self):
        if self._process is not None:
            self.terminations += 1
        self._process = None


def test_latest_only_queue_before_start_and_close_is_terminal(tmp_path, view, monkeypatch):
    results, statuses = [], []
    client = ControlledClient(tmp_path, results, statuses)
    monkeypatch.setattr(client, "start", lambda: None)
    _submit(client, view)
    newer = replace(view, state_version=8)
    _submit(client, newer)
    assert client._requests.qsize() == 1
    assert client._requests.get_nowait()[0].view == newer
    client.close()
    with pytest.raises(ModelBackendError):
        _submit(client, view)


def test_newest_state_replaces_pending_and_drops_old_result(tmp_path, view):
    results, statuses = [], []
    client = ControlledClient(tmp_path, results, statuses)
    try:
        _submit(client, view)
        wait_until(lambda: len(client.sent) == 1)
        _submit(client, replace(view, state_version=8))
        _submit(client, replace(view, state_version=9))
        client._responses.put(reply(client.sent[0]))
        wait_until(lambda: len(client.sent) == 2)
        assert client.sent[1]["state_version"] == 9
        client._responses.put(reply(client.sent[1]))
        wait_until(lambda: len(results) == 1)
        assert isinstance(results[0], CanonicalAdviceResult)
        assert results[0].state_version == 9
        assert any(s.get("event") == "stale_advice_dropped" for s in statuses)
    finally:
        client.close()


def test_same_state_key_new_request_drops_old_result(tmp_path, view):
    results, statuses = [], []
    client = ControlledClient(tmp_path, results, statuses)
    try:
        _submit(client, view)
        wait_until(lambda: len(client.sent) == 1)
        first = client.sent[0]
        _submit(client, view)
        client._responses.put(reply(first))
        wait_until(lambda: len(client.sent) == 2)
        assert client.sent[1]["request_id"] != first["request_id"]
        assert not results
        client._responses.put(reply(client.sent[1]))
        wait_until(lambda: len(results) == 1)
        assert isinstance(results[0], CanonicalAdviceResult)
        assert results[0].request_id == client.sent[1]["request_id"]
        assert any(s.get("event") == "stale_advice_dropped" for s in statuses)
    finally:
        client.close()


def test_invalidate_and_resubmit_same_key_cannot_resurrect_old_result(tmp_path, view):
    results, statuses = [], []
    client = ControlledClient(tmp_path, results, statuses)
    try:
        _submit(client, view)
        wait_until(lambda: len(client.sent) == 1)
        client.invalidate()
        _submit(client, view)
        client._responses.put(reply(client.sent[0]))
        wait_until(lambda: len(client.sent) == 2)
        assert not results
        assert client.sent[0]["request_id"] != client.sent[1]["request_id"]
        client._responses.put(reply(client.sent[1]))
        wait_until(lambda: len(results) == 1)
        assert isinstance(results[0], CanonicalAdviceResult)
        assert results[0].request_id == client.sent[1]["request_id"]
    finally:
        client.close()


@pytest.mark.parametrize("field", ["request_id", "session_id", "game_id", "state_version"])
@pytest.mark.parametrize("location", ["envelope", "result"])
def test_every_response_identity_is_verified(tmp_path, view, field, location):
    results, statuses = [], []
    client = ControlledClient(tmp_path, results, statuses)
    try:
        _submit(client, view)
        wait_until(lambda: bool(client.sent))
        message = reply(client.sent[0])
        target = message if location == "envelope" else message["result"]
        target[field] = 99 if field == "state_version" else "wrong"
        client._responses.put(message)
        wait_until(lambda: bool(statuses))
        assert not results
        assert statuses[-1]["error_code"] == "RESPONSE_MISMATCH"
    finally:
        client.close()


def test_timeout_restarts_same_source_on_next_observation(tmp_path, view):
    results, statuses = [], []
    client = ControlledClient(tmp_path, results, statuses)
    try:
        _submit(client, view)
        wait_until(lambda: any(s.get("model_status") == "timeout" for s in statuses))
        assert client.terminations == 1
        _submit(client, replace(view, state_version=8))
        wait_until(lambda: len(client.sent) == 2)
        assert client.launches == 2
        assert client.source.name == "synthetic.npz"
        client._responses.put(reply(client.sent[1]))
        wait_until(lambda: len(results) == 1)
    finally:
        client.close()


def test_close_drops_inflight_result_and_stops_thread(tmp_path, view):
    results, statuses = [], []
    client = ControlledClient(tmp_path, results, statuses)
    _submit(client, view)
    wait_until(lambda: bool(client.sent))
    thread = client._thread
    closer = threading.Thread(target=client.close)
    closer.start()
    wait_until(lambda: client._stop.is_set())
    client._responses.put(reply(client.sent[0]))
    closer.join(timeout=3)
    assert not closer.is_alive()
    assert not thread.is_alive()
    assert not results


def test_invalid_public_input_is_not_sent(tmp_path, view):
    results, statuses = [], []
    client = ControlledClient(tmp_path, results, statuses)
    _submit(client, replace(view, visibility="private"))
    assert not client.sent
    assert client.launches == 0
    assert statuses[-1]["error_code"] == "MODEL_INPUT_UNSUPPORTED"
    client.close()


@pytest.mark.parametrize("source_kind", ["legacy", "manifest", "directory"])
def test_real_subprocess_generic_client_with_synthetic_artifact(npz_path, manifest_path, view, source_kind):
    source = {"legacy": npz_path, "manifest": manifest_path, "directory": manifest_path.parent}[source_kind]
    results, statuses = [], []
    client = InferenceClient(source=source, on_result=results.append, on_status=statuses.append,
                             timeout_seconds=3)
    try:
        _submit(client, view)
        wait_until(lambda: bool(results) or any(s.get("model_status") == "error" for s in statuses), timeout=15)
        assert len(results) == 1, statuses
        assert isinstance(results[0], CanonicalAdviceResult)
        assert results[0].provider == "fabledan"
        assert results[0].state_version == view.state_version
        assert any(s.get("model_status") == "ready" for s in statuses)
    finally:
        client.close()
    assert client._process is None


def test_real_missing_model_never_reports_ready(tmp_path, view):
    results, statuses = [], []
    client = InferenceClient(tmp_path / "missing.npz", on_result=results.append, on_status=statuses.append)
    try:
        _submit(client, view)
        wait_until(lambda: any(s.get("model_status") == "error" for s in statuses), timeout=15)
        assert not results
        assert not any(s.get("model_status") == "ready" for s in statuses)
        assert statuses[-1]["error_code"] == "ARTIFACT_MISSING"
    finally:
        client.close()


def test_startup_load_failure_does_not_switch_and_next_submit_can_retry(tmp_path, view):
    class FailFirstClient(ControlledClient):
        def _launch(self):
            if self.launches == 0:
                self.launches += 1
                self.on_status({"model_status": "error", "error_code": "MODEL_LOAD_FAILED"})
                return False
            return super()._launch()
    results, statuses = [], []
    client = FailFirstClient(tmp_path, results, statuses)
    try:
        client.start()
        wait_until(lambda: bool(statuses))
        _submit(client, view)
        wait_until(lambda: len(client.sent) == 1)
        assert client.launches == 2
        client._responses.put(reply(client.sent[0]))
        wait_until(lambda: len(results) == 1)
    finally:
        client.close()


def test_callback_does_not_hold_transport_mutex_during_concurrent_submit(tmp_path, view):
    results, statuses = [], []
    client = ControlledClient(tmp_path, results, statuses)
    entered, release, submitted = threading.Event(), threading.Event(), threading.Event()
    def callback(result):
        entered.set()
        release.wait(timeout=3)
        results.append(result)
    client.on_result = callback
    try:
        _submit(client, view)
        wait_until(lambda: bool(client.sent))
        client._responses.put(reply(client.sent[0]))
        assert entered.wait(timeout=2)
        def submit():
            _submit(client, replace(view, state_version=8))
            submitted.set()
        thread = threading.Thread(target=submit, daemon=True)
        thread.start()
        completed = submitted.wait(timeout=1)
        release.set()
        thread.join(timeout=2)
        assert completed, "Result callback held the transport mutex and blocked submission"
    finally:
        release.set()
        client.close()


def test_selected_loading_loaded_source_fingerprint_is_consistent(npz_path, view):
    import hashlib
    results, statuses = [], []
    client = InferenceClient(npz_path, on_result=results.append, on_status=statuses.append,
                             timeout_seconds=3)
    expected = hashlib.sha256(npz_path.read_bytes()).hexdigest()
    try:
        _submit(client, view)
        wait_until(lambda: bool(results) or any(s.get("model_status") == "error" for s in statuses),
                   timeout=15)
        loading = next(s for s in statuses if s.get("model_status") == "loading"
                       and s.get("metadata", {}).get("bundle_fingerprint"))
        ready = next(s for s in statuses if s.get("model_status") == "ready")
        assert client.source == npz_path.resolve()
        assert client.source_fingerprint == expected
        assert loading["metadata"]["bundle_fingerprint"] == expected
        assert ready["metadata"]["bundle_fingerprint"] == expected
    finally:
        client.close()


def test_optional_marvel_source_is_not_selected_for_fabledan(npz_path, monkeypatch):
    monkeypatch.setenv("GUANCE_MARVEL_V2_SOURCE", str(npz_path.parent / "unrelated"))
    client = InferenceClient(npz_path, on_result=lambda result: None,
                             on_status=lambda status: None)
    try:
        assert client.selected_provider == "fabledan"
        assert client.source_fingerprint
    finally:
        client.close()


def test_worker_bootstrap_sanitizes_python_paths_before_start(tmp_path, monkeypatch):
    inherited = str(tmp_path / "inherited-pythonpath")
    untrusted = str(tmp_path / "unverified-marvel-source")
    monkeypatch.setenv("PYTHONPATH", inherited)
    monkeypatch.setenv("PYTHONHOME", str(tmp_path / "fake-home"))
    monkeypatch.setenv("GUANCE_MARVEL_V2_SOURCE", untrusted)
    client = InferenceClient(tmp_path / "missing-manifest.json",
                             on_result=lambda result: None, on_status=lambda status: None)
    try:
        command, env, _flags = client._worker_process_spec()
        assert command[1:4] == ["-I", "-X", "utf8"]
        assert "-c" in command and "runpy.run_module('guance'" in command[command.index("-c") + 1]
        assert inherited not in command and untrusted not in command
        assert "PYTHONPATH" not in env and "PYTHONHOME" not in env
        assert env["PYTHONNOUSERSITE"] == "1" and env["PYTHONSAFEPATH"] == "1"
        assert env["GUANCE_MARVEL_V2_SOURCE"] == untrusted  # data only, never an import path
    finally:
        client.close()


def test_fabledan_worker_ignores_untrusted_pythonpath_customizers(
        npz_path, view, tmp_path, monkeypatch):
    malicious = tmp_path / "fabledan-shadow"
    malicious.mkdir()
    sentinel = tmp_path / "fabledan-bootstrap-sentinel.txt"
    payload = f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('EXECUTED', encoding='utf-8')\n"
    for name in ("sitecustomize.py", "usercustomize.py", "guance.py", "numpy.py", "json.py"):
        (malicious / name).write_text(payload, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(malicious))
    monkeypatch.setenv("GUANCE_MARVEL_V2_SOURCE", str(malicious))
    results, statuses = [], []
    client = InferenceClient(npz_path, on_result=results.append, on_status=statuses.append,
                             timeout_seconds=3)
    try:
        _submit(client, view)
        wait_until(lambda: bool(results) or any(s.get("model_status") == "error" for s in statuses),
                   timeout=15)
        assert len(results) == 1, statuses
        assert results[0].provider == "fabledan"
        assert client.selected_provider == "fabledan"
        assert not sentinel.exists()
    finally:
        client.close()
    assert not sentinel.exists()


def test_worker_handshake_timeout_is_bounded_and_reported_as_model_timeout(tmp_path):
    import os
    import sys

    class HangingClient(InferenceClient):
        def _worker_process_spec(self):
            script = (
                "import json,sys,time;"
                "ids=json.loads(sys.stdin.readline());"
                "payload={'protocol_version':3,'type':'status',"
                "'request_id':ids['request_id'],'session_id':ids.get('session_id',''),"
                "'game_id':ids.get('game_id',''),'state_version':ids.get('state_version',0),"
                "'epoch':ids.get('epoch',0),'generation':ids.get('generation',0),"
                "'model_status':'loading'};"
                "print(json.dumps(payload),flush=True);"
                "time.sleep(5)"
            )
            return [sys.executable, "-c", script], os.environ.copy(), 0

    statuses: list[dict] = []
    results: list = []
    client = HangingClient(tmp_path / "synthetic.npz", on_result=results.append,
                           on_status=statuses.append, timeout_seconds=1,
                           load_timeout_seconds=0.3)
    try:
        client.start()
        wait_until(lambda: any(item.get("model_status") in {"error", "unavailable", "timeout"}
                               for item in statuses), timeout=10)
        assert statuses[-1]["model_status"] == "error"
        assert statuses[-1]["error_code"] == "MODEL_TIMEOUT"
        assert not results
    finally:
        client.close()


@pytest.mark.parametrize("value", [0, -1, 0.01, 601, True, "180"])
def test_load_timeout_argument_is_bounded(tmp_path, value):
    with pytest.raises(ModelBackendError) as exc:
        InferenceClient(tmp_path / "synthetic.npz", on_result=lambda _r: None,
                        on_status=lambda _s: None, load_timeout_seconds=value)
    assert exc.value.code == "MODEL_INPUT_INVALID"


def test_worker_launch_carries_the_selected_device(tmp_path):
    for device in ("auto", "cpu", "cuda"):
        client = InferenceClient(tmp_path / "synthetic.npz", on_result=lambda _r: None,
                                 on_status=lambda _s: None, requested_device=device)
        try:
            command, _env, _flags = client._worker_process_spec()
            assert command[-2:] == ["--device", device]
            assert any("sys.dont_write_bytecode=True" in part for part in command)
        finally:
            client.close()
    with pytest.raises(ModelBackendError) as exc:
        InferenceClient(tmp_path / "synthetic.npz", on_result=lambda _r: None,
                        on_status=lambda _s: None, requested_device="gpu")
    assert exc.value.code == "MODEL_INPUT_INVALID"
