"""Latest-only model transport with bounded codec and full request identity guards."""
from __future__ import annotations

import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import uuid
from typing import Any

from guance.contracts import (CanonicalAdviceResult, CanonicalPublicGameView, GameObservation,
                               ModelStatusSnapshot, validate_advice_for_view)
from guance.ports.model import ModelBackendError, ModelSource, ResultCallback, StatusCallback, public_error
from .codec import IDENTITY_FIELDS, dumps, envelope, read_message, safe_metadata
from .manifest import MAX_MANIFEST_BYTES, sha256_file, strict_json

_WORKER_BOOTSTRAP = (
    "import runpy,sys;"
    "trusted_root=sys.argv.pop(1);"
    "sys.path.insert(0,trusted_root);"
    "runpy.run_module('guance',run_name='__main__')"
)


def _selected_source_fingerprint(source: Path) -> str:
    """Bounded identity for selected/loading/loaded consistency; never scans directories."""
    try:
        if source.suffix.lower() == ".npz":
            return sha256_file(source)
        manifest = source / "manifest.json" if source.is_dir() else source
        if manifest.suffix.lower() != ".json" or not manifest.is_file():
            return ""
        raw = manifest.read_bytes()
        if len(raw) > MAX_MANIFEST_BYTES:
            return ""
        data = strict_json(raw.decode("utf-8"))
        value = data.get("artifact_sha256") if isinstance(data, dict) else None
        return value if isinstance(value, str) and len(value) == 64 else ""
    except Exception:
        return ""


def _selected_source_provider(source: Path) -> str:
    if source.suffix.lower() == ".npz":
        return "fabledan"
    try:
        manifest = source / "manifest.json" if source.is_dir() else source
        if manifest.suffix.lower() != ".json" or not manifest.is_file():
            return ""
        raw = manifest.read_bytes()
        if len(raw) > MAX_MANIFEST_BYTES:
            return ""
        data = strict_json(raw.decode("utf-8"))
        value = data.get("provider") if isinstance(data, dict) else None
        return value if isinstance(value, str) and len(value) <= 128 else ""
    except Exception:
        return ""


class InferenceClient:
    def __init__(self, weights_path: ModelSource | None = None, *, source: ModelSource | None = None,
                 on_result: ResultCallback, on_status: StatusCallback,
                 timeout_seconds: float = 2.0, threads: int = 2) -> None:
        if source is not None and weights_path is not None:
            raise ValueError("Specify one model source")
        selected = source if source is not None else weights_path
        if not isinstance(selected, (Path, str)) or not str(selected).strip():
            raise ModelBackendError("MODEL_SOURCE_INVALID")
        self.source = Path(selected).resolve()
        self.weights_path = self.source  # Explicit legacy constructor compatibility.
        self.source_fingerprint = _selected_source_fingerprint(self.source)
        self.selected_provider = _selected_source_provider(self.source)
        self.on_result, self.on_status = on_result, on_status
        self.timeout = max(0.2, timeout_seconds)
        self.threads = max(1, min(threads, 4))
        self._requests: queue.Queue[tuple[Any, CanonicalPublicGameView, str] | None] = queue.Queue(maxsize=1)
        self._responses: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=16)
        self._stop = threading.Event()
        self._process: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        self._latest_key: tuple[str, str, int] | None = None
        self._latest_identity: dict[str, Any] | None = None
        self._epoch = 0
        self._generation = 0
        # Compatibility for injected transport tests whose _launch override models
        # an already-loaded provider. The concrete _launch always resets this false
        # until worker metadata proves availability.
        self._model_available = True
        self._loaded_metadata: dict[str, Any] = {}
        self._graceful_shutdown = False
        self._closed = False
        self._lock = threading.RLock()
        self._process_lock = threading.RLock()

    def _notify_status(self, payload: dict[str, Any]) -> None:
        self.on_status(ModelStatusSnapshot.from_wire(payload))

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise ModelBackendError("MODEL_CLOSED")
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name="guance-model", daemon=True)
            self._thread.start()

    def submit(self, observation: GameObservation | CanonicalPublicGameView) -> str | None:
        try:
            view = (CanonicalPublicGameView.from_observation(observation)
                    if isinstance(observation, GameObservation) else observation)
            view = CanonicalPublicGameView.from_dict(view.to_dict())
        except Exception as exc:
            self.invalidate()
            self._notify_status({"model_status": "error", **public_error(exc, "MODEL_INPUT_INVALID")})
            return None
        request_id = uuid.uuid4().hex
        with self._lock:
            if self._closed:
                raise ModelBackendError("MODEL_CLOSED")
            self._generation += 1
            request_identity = {
                "request_id": request_id, "session_id": view.session_id, "game_id": view.game_id,
                "state_version": view.state_version, "epoch": self._epoch,
                "generation": self._generation,
            }
            self._latest_identity = dict(request_identity)
            self._latest_key = view.key
            self._drain_requests()
            self._requests.put_nowait((request_identity, view, request_id))
        self.start()
        return request_id

    def _drain_requests(self) -> None:
        try:
            self._requests.get_nowait()
        except queue.Empty:
            pass

    def invalidate(self) -> None:
        with self._lock:
            self._epoch += 1
            self._generation += 1
            self._latest_key = None
            self._latest_identity = None
            self._drain_requests()

    def _send(self, item: dict[str, Any]) -> None:
        with self._process_lock:
            process = self._process
            if self._stop.is_set() or process is None or process.stdin is None or process.poll() is not None:
                raise ModelBackendError("MODEL_UNAVAILABLE")
            process.stdin.write(dumps(item))
            process.stdin.flush()

    def _worker_process_spec(self) -> tuple[list[str], dict[str, str], int]:
        """Build a worker launch spec without trusting selected-provider paths.

        Source mode uses Python isolated mode.  The only pre-start import path is
        Guance's own source root; optional provider source is verified and loaded
        later inside the worker.  Frozen mode likewise drops path-injection env.
        """
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
                   PYTHONNOUSERSITE="1", PYTHONSAFEPATH="1")
        for key in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
            env[key] = str(self.threads)
        if getattr(sys, "frozen", False):
            command = [sys.executable]
        else:
            trusted_root = str(Path(__file__).resolve().parents[3])
            command = [sys.executable, "-I", "-X", "utf8", "-c", _WORKER_BOOTSTRAP, trusted_root]
        command += ["--model-worker", "--threads", str(self.threads)]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        return command, env, flags

    def _launch(self) -> bool:
        if self._stop.is_set():
            return False
        loading_metadata = ({"bundle_fingerprint": self.source_fingerprint}
                            if self.source_fingerprint else {})
        self._notify_status({"model_status": "loading", "message": "正在加载本地模型",
                             "metadata": loading_metadata})
        self._model_available = False
        self._loaded_metadata = {}
        command, env, flags = self._worker_process_spec()
        responses: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=16)
        self._responses = responses
        try:
            with self._process_lock:
                if self._stop.is_set():
                    return False
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="strict",
                    bufsize=1, env=env, creationflags=flags)
                self._process = process

            def read() -> None:
                try:
                    assert process.stdout is not None
                    while True:
                        message = read_message(process.stdout)
                        if message is None:
                            break
                        responses.put(message, timeout=1)
                except Exception:
                    try:
                        responses.put_nowait({"type": "reader_error"})
                    except queue.Full:
                        pass
                finally:
                    try:
                        responses.put_nowait({"type": "exited"})
                    except queue.Full:
                        pass

            threading.Thread(target=read, name="guance-model-reader", daemon=True).start()
            with self._lock:
                load_epoch, load_generation = self._epoch, self._generation
            load_ids = {"request_id": uuid.uuid4().hex, "session_id": "", "game_id": "",
                        "state_version": 0, "epoch": load_epoch, "generation": load_generation}
            self._send(envelope("load", load_ids, source=str(self.source)))
            deadline = time.monotonic() + 12
            while True:
                first = responses.get(timeout=max(0.001, deadline - time.monotonic()))
                if first.get("type") in {"reader_error", "exited"}:
                    raise ModelBackendError("MODEL_UNAVAILABLE")
                if any(first.get(name) != load_ids[name] for name in IDENTITY_FIELDS):
                    raise ModelBackendError("RESPONSE_MISMATCH")
                if first.get("type") == "status" and first.get("model_status") == "loading":
                    if time.monotonic() >= deadline:
                        raise ModelBackendError("MODEL_TIMEOUT")
                    continue
                if first.get("type") == "error":
                    raise ModelBackendError(first.get("error_code", "MODEL_LOAD_FAILED"))
                if first.get("type") != "loaded" or not isinstance(first.get("metadata"), dict):
                    raise ModelBackendError("PROTOCOL_INVALID")
                if self._stop.is_set():
                    self._terminate()
                    return False
                metadata = safe_metadata(first["metadata"])
                if (self.source_fingerprint
                        and metadata.get("bundle_fingerprint") != self.source_fingerprint):
                    raise ModelBackendError("RESPONSE_MISMATCH")
                self._loaded_metadata = metadata
                loaded_status = str(metadata.get("status", "loaded"))
                self._model_available = loaded_status in {"loaded", "ready"}
                if self._model_available:
                    self._notify_status({"model_status": "ready", "message": "本地模型已就绪",
                                         "metadata": metadata})
                else:
                    self._notify_status({"model_status": "unavailable",
                                         "error_code": "GUANDAN_STATE_UNAVAILABLE",
                                         "message": "当前公开局面尚不满足该实验模型的输入条件。",
                                         "metadata": metadata})
                return True
        except Exception as exc:
            if not self._stop.is_set():
                error = public_error(exc, "MODEL_LOAD_FAILED")
                status = "unavailable" if error["error_code"] == "MODEL_UNAVAILABLE" else "error"
                self._notify_status({"model_status": status, **error})
            self._terminate()
            return False

    def _run(self) -> None:
        try:
            with self._lock:
                startup_generation = self._generation
            if not self._launch():
                with self._lock:
                    if self._generation == startup_generation:
                        self._drain_requests()
            while not self._stop.is_set():
                try:
                    pending = self._requests.get(timeout=0.2)
                except queue.Empty:
                    continue
                if pending is None:
                    break
                request_identity, view, request_id = pending
                with self._lock:
                    current = (request_identity == self._latest_identity
                               and view.key == self._latest_key and not self._closed)
                if not current:
                    continue
                ids = dict(request_identity)
                try:
                    if self._process is None or self._process.poll() is not None:
                        if not self._launch():
                            continue
                    if not self._model_available:
                        self._notify_status({"model_status": "unavailable",
                            "error_code": "GUANDAN_STATE_UNAVAILABLE",
                            "message": "当前公开局面尚不满足该实验模型的输入条件。",
                            "metadata": self._loaded_metadata})
                        continue
                    self._send(envelope("recommend", ids, observation=view.to_dict()))
                    started = time.monotonic()
                    reply = self._responses.get(timeout=self.timeout)
                    if any(reply.get(name) != ids[name] for name in IDENTITY_FIELDS):
                        raise ModelBackendError("RESPONSE_MISMATCH")
                    if reply.get("type") == "error":
                        raise ModelBackendError(reply.get("error_code", "MODEL_INFERENCE_FAILED"))
                    if reply.get("type") != "result":
                        raise ModelBackendError("PROTOCOL_INVALID")
                    from guance.contracts import decode_canonical_advice
                    result = decode_canonical_advice(reply.get("result"))
                    if result.key != view.key or result.request_id != ids["request_id"]:
                        raise ModelBackendError("RESPONSE_MISMATCH")
                    validate_advice_for_view(view, result)
                    if (result.status != "ready" or result.error_code or result.error_message):
                        raise ModelBackendError("MODEL_OUTPUT_INVALID")
                    with self._lock:
                        current = (request_identity == self._latest_identity
                                   and view.key == self._latest_key and not self._closed)
                        deliver = current and time.monotonic() - started <= self.timeout
                        closed = self._closed
                    if deliver:
                        self.on_result(result)
                    elif not closed:
                        self._notify_status({"event": "stale_advice_dropped", "message": "已丢弃过期推荐"})
                except queue.Empty:
                    if not self._stop.is_set():
                        self._notify_status({"model_status": "timeout", "error_code": "MODEL_TIMEOUT",
                                        "message": "Model inference timed out."})
                    self._terminate()
                except Exception as exc:
                    if not self._stop.is_set():
                        self._notify_status({"model_status": "error", **public_error(exc)})
                    self._terminate()
        finally:
            self._terminate()
            with self._lock:
                self._thread = None

    def _terminate(self) -> None:
        self._model_available = False
        self._loaded_metadata = {}
        with self._process_lock:
            process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.poll() is None and self._graceful_shutdown and process.stdin is not None:
                try:
                    close_ids = {
                        "request_id": uuid.uuid4().hex, "session_id": "", "game_id": "",
                        "state_version": 0, "epoch": self._epoch, "generation": self._generation,
                    }
                    process.stdin.write(dumps(envelope("close", close_ids)))
                    process.stdin.flush()
                    process.stdin.close()
                    process.wait(timeout=0.8)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=0.8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=0.8)
        except (OSError, subprocess.TimeoutExpired):
            pass
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._graceful_shutdown = True
            self._stop.set()
            self.invalidate()
            try:
                self._requests.put_nowait(None)
            except queue.Full:
                pass
            thread = self._thread
        self._terminate()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)


def default_client_factory(source: ModelSource, *, on_result: ResultCallback,
                           on_status: StatusCallback, timeout_seconds: float,
                           threads: int) -> InferenceClient:
    """Composition-root factory for the concrete JSONL client."""
    return InferenceClient(source, on_result=on_result, on_status=on_status,
                           timeout_seconds=timeout_seconds, threads=threads)
