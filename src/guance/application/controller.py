"""Qt-agnostic application controller connecting runtime, state, and model."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from typing import Any

from guance.config import app_paths, default_model_path, ensure_app_paths
from guance.contracts import EngineUpdate, SourceFrame
from guance.domain.session import SessionEngine
from guance.diagnostics import DiagnosticLog
from guance.infrastructure.capture.diagnostics import BoundedDiagnostics
from guance.infrastructure.model.client import InferenceClient
from guance.infrastructure.protocol.saved_capture import iter_game_frames
from guance.infrastructure.windows.certificate import WindowsCertificateManager
from guance.infrastructure.windows.proxy import WindowsProxyBackend
from guance.application.runtime.service import RuntimeOptions, RuntimeService
from guance.ports.capture import CaptureMode, RuntimePaths, UpstreamProxy


class GuanceController:
    def __init__(self, viewmodel: Any, *, paths: dict[str, Path] | None = None):
        self.viewmodel = viewmodel
        self.paths = paths or app_paths()
        ensure_app_paths(self.paths)
        self.log = DiagnosticLog(self.paths["logs_dir"])
        self.session = SessionEngine("desktop")
        self._lock = threading.RLock()
        self._runtime: RuntimeService | None = None
        self._model = InferenceClient(default_model_path(self.paths), on_result=self._on_advice,
                                      on_status=self._on_model_status, threads=2)
        self._attach_signals()

    def _attach_signals(self) -> None:
        self.viewmodel.startRequested.connect(self.start)
        self.viewmodel.stopRequested.connect(self.stop)
        self.viewmodel.authorizeRequested.connect(self.authorize)
        self.viewmodel.repairRequested.connect(self.repair)
        self.viewmodel.removeCertificateRequested.connect(self.remove_certificate)
        self.viewmodel.exportDiagnosticsRequested.connect(self.export_diagnostics)

    def _mode_options(self) -> RuntimeOptions:
        helper = self.paths["runtime_dir"] / "mitmproxy" / "mitmdump.exe"
        if not helper.is_file():
            helper = Path(r"C:\Program Files\mitmproxy\bin\mitmdump.exe")
        addon = self.paths["runtime_dir"] / "mitmproxy" / "addon.py"
        if not addon.is_file():
            addon = Path(__file__).resolve().parents[1] / "infrastructure" / "capture" / "addon.py"
        proxy = WindowsProxyBackend().snapshot()
        upstream = None
        mode = CaptureMode.DIRECT
        if proxy.enabled and proxy.server:
            server = proxy.server.split(";", 1)[0]
            if "=" in server:
                server = server.split("=", 1)[1]
            if ":" in server:
                host, port_text = server.rsplit(":", 1)
                try:
                    port = int(port_text)
                except ValueError:
                    port = 0
                if port:
                    upstream = UpstreamProxy(kind="mixed", host=host.strip("[]"), port=port)
                    mode = CaptureMode.UPSTREAM_MIXED
        return RuntimeOptions(
            mode=mode, upstream=upstream, helper_path=helper, addon_path=addon,
            allowed_hosts=("hlxyxws.huanle.qq.com", "hlsjwshall.huanle.qq.com",
                           "long.wxagame.weixin.qq.com", "guandangamelog.huanle.qq.com"),
            require_certificate=True,
        )

    def _build_runtime(self) -> RuntimeService:
        options = self._mode_options()
        paths = RuntimePaths.from_mapping(self.paths)
        runtime = RuntimeService(paths, on_frame=self._on_frame, on_status=self._on_runtime_status,
                                 options=options, diagnostics=BoundedDiagnostics())
        return runtime

    def _ensure_certificate_material(self) -> None:
        """Start the helper briefly so mitmproxy creates its per-install CA.

        This never changes Windows proxy settings. Trust is installed only later
        by RuntimeService after the user has explicitly consented.
        """
        options = self._mode_options()
        helper = options.helper_path
        if helper is None or not helper.is_file():
            raise RuntimeError("采集组件未安装")
        conf = self.paths["config_dir"] / "mitmproxy"
        conf.mkdir(parents=True, exist_ok=True)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        process = subprocess.Popen([str(helper), "--mode", "regular", "--listen-host", "127.0.0.1",
                                    "--listen-port", "18080", "--set", f"confdir={conf}",
                                    "--quiet"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=flags)
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                cert = conf / "mitmproxy-ca-cert.cer"
                if cert.is_file():
                    return
                if process.poll() is not None:
                    raise RuntimeError("采集组件无法生成本机证书")
                time.sleep(.1)
            raise RuntimeError("本机证书准备超时")
        finally:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=2)
                except subprocess.TimeoutExpired: process.kill()

    def authorize(self, consent: bool) -> None:
        if not consent:
            self._status({"phase":"idle","busy":False,"title":"尚未连接","detail":"未授权，网络设置没有修改。"})
            return
        try:
            self._ensure_certificate_material()
            if self._runtime is None:
                self._runtime=self._build_runtime()
            result=self._runtime.prepare_certificate(True)
            if not result.get("ok"):
                self._status({"phase":"error","busy":False,"error_message":result.get("message","证书初始化失败"),"detail":"本机网络组件未准备完成。"})
                return
            self.start()
        except Exception as exc:
            self.log.write("certificate_prepare_failed", level="ERROR", message=type(exc).__name__)
            self._status({"phase":"error","busy":False,"error_message":"网络组件初始化失败","detail":"请打开诊断查看处理建议。"})

    def start(self) -> None:
        try:
            if self._runtime is None:self._runtime=self._build_runtime()
            result=self._runtime.start()
            if result.get("ok"):
                self._model.start()
                self._status({"phase":"waiting","running":True,"connected":True,"busy":False,"network_mode":result.get("mode","direct"),"detail":"采集已启动，请进入大掼蛋对局。"})
            else:
                self._status({"phase":"error","running":False,"connected":False,"busy":False,"error_message":result.get("message","采集启动失败"),"detail":"当前无法安全启动采集。"})
        except Exception as exc:
            self.log.write("runtime_start_failed", level="ERROR", message=type(exc).__name__)
            self._status({"phase":"error","running":False,"connected":False,"busy":False,"error_message":"采集服务启动失败","detail":"请打开诊断。"})

    def stop(self) -> None:
        if self._runtime is not None:self._runtime.stop()
        self._model.invalidate();self._model.close();self._status({"phase":"stopped","running":False,"connected":False,"busy":False})

    def repair(self) -> None:
        result=self._runtime.repair() if self._runtime else {"ok":True}
        self._status({"phase":"idle" if result.get("ok") else "error","detail":result.get("message","修复完成"),"error_message":"" if result.get("ok") else result.get("message","")})

    def remove_certificate(self, consent: bool) -> None:
        result=self._runtime.remove_certificate(consent) if self._runtime else {"ok":False,"message":"助手尚未初始化"}
        self._status({"phase":"idle","detail":result.get("message","证书处理完成"),"certificate_state":"missing" if result.get("ok") else "error"})

    def export_diagnostics(self) -> None:
        destination=self.paths["exports_dir"] / f"diagnostics-{int(time.time())}.zip"
        self.log.export(destination, status=self._runtime.inspect() if self._runtime else {})
        self.viewmodel.append_log({"level":"INFO","code":"DIAGNOSTICS_EXPORTED","message":f"诊断包已生成：{destination.name}"})

    def _on_frame(self, frame: SourceFrame) -> None:
        for update in self.session.feed(frame):
            self._handle_update(update)

    def _handle_update(self, update: EngineUpdate) -> None:
        if update.kind == "error":
            self._model.invalidate();self._status({"phase":"error","error_message":update.message,"detail":"局面无法安全同步，推荐已暂停。"});return
        if update.kind == "state":
            self._status({"phase":"ready" if update.data.get("phase")=="active" else "waiting","connected":True,"running":True,"detail":"局面正在同步。","diagnostics":{"state_version":update.data.get("state_version",0)}})
        if update.kind == "observation" and update.observation:
            data=update.observation.to_dict();self.viewmodel.apply_observation(data)
            if update.observation.current_seat == update.observation.my_seat:
                self._status({"phase":"thinking","connected":True,"running":True})
                self._model.submit(update.observation)

    def _on_advice(self, result: dict) -> None:
        self.viewmodel.apply_advice(result);self._status({"phase":"ready","connected":True,"running":True})

    def _on_model_status(self, status: dict) -> None:
        if status.get("model_status") == "ready":
            self._status({"model_state":"ready","model_summary":"本地模型已就绪"})
        elif status.get("model_status") == "error":
            self._status({"phase":"error","model_state":"error","error_message":status.get("message","模型异常")})

    def _on_runtime_status(self, status: dict) -> None:
        if status.get("state") in {"error","stop_error"}:
            self._status({"phase":"error","error_message":status.get("message","采集异常")})
        self.viewmodel.append_log({"level":"INFO","code":str(status.get("state","RUNTIME")),"message":str(status.get("message",status.get("state","")))})

    def _status(self, data: dict) -> None:
        self.viewmodel.apply_status(data)

    def close(self) -> None:
        try:self.stop()
        except Exception:pass
