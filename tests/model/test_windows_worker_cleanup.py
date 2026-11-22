"""Windows tests for model workers whose child outlives their own process."""

import os
import subprocess
import sys
import threading

import pytest

from guance.infrastructure.model.client import InferenceClient


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows process jobs")


WORKER_SCRIPT = """
import json
from pathlib import Path
import subprocess
import sys
import time

request = json.loads(sys.stdin.readline())
child = subprocess.Popen(
    [sys.executable, "-I", "-c", "import time; time.sleep(45)"],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
Path(sys.argv[1]).write_text(str(child.pid), encoding="ascii")
request.pop("source")
request.update(type="loaded", metadata={"provider": "danks_v3", "status": "loaded"})
print(json.dumps(request), flush=True)
sys.stdin.readline()
if sys.argv[2] == "forced-close":
    time.sleep(45)
"""


@pytest.mark.parametrize("exit_mode", ["close", "forced-close", "worker-exit"])
def test_danks_worker_reaps_child(tmp_path, monkeypatch, exit_mode):
    import win32api
    import win32con
    import win32event

    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"provider": "danks_v3"}', encoding="utf-8")
    worker = tmp_path / "worker.py"
    worker.write_text(WORKER_SCRIPT, encoding="utf-8")
    pid_path = tmp_path / "child.pid"
    ready = threading.Event()

    def on_status(status):
        if status.get("model_status") == "ready":
            ready.set()

    client = InferenceClient(
        manifest, on_result=lambda result: None, on_status=on_status,
        load_timeout_seconds=8)
    monkeypatch.setattr(client, "_worker_process_spec", lambda: (
        [sys.executable, "-I", "-B", str(worker), str(pid_path), exit_mode],
        os.environ.copy(), subprocess.CREATE_NO_WINDOW))

    child = None
    try:
        client.start()
        assert ready.wait(8), "test worker did not load"
        child = win32api.OpenProcess(
            win32con.SYNCHRONIZE | win32con.PROCESS_TERMINATE,
            False, int(pid_path.read_text(encoding="ascii")))
        assert win32event.WaitForSingleObject(child, 0) == win32event.WAIT_TIMEOUT
        if exit_mode == "worker-exit":
            client._process.kill()
        else:
            client.close()
        assert win32event.WaitForSingleObject(child, 3000) == win32event.WAIT_OBJECT_0
    finally:
        # Keep a baseline failure or interrupted assertion from leaving the fixture alive.
        if child is not None:
            if win32event.WaitForSingleObject(child, 0) == win32event.WAIT_TIMEOUT:
                win32api.TerminateProcess(child, 1)
            child.Close()
        client.close()
