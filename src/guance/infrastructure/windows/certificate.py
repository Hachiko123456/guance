"""Certificate consent boundary for mitmproxy's local CA.

The default implementation deliberately does not import or remove a
certificate.  A Windows integration can inject a provisioner/remover that
shows its own consent UI and performs the operation.  This keeps tests and
non-Windows imports side-effect free and makes it impossible for the runtime to
silently install a CA.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from guance.infrastructure.capture.diagnostics import redact_value
from guance.ports.capture import CertificateConsentRequest, CertificateStatus


CertificateChecker = Callable[[Path], bool | str | CertificateStatus]
CertificateProvisioner = Callable[[Path, int], Any]
CertificateRemover = Callable[[Path, int], Any]
_CERTUTIL_TIMEOUT_SECONDS = 15.0



def _certutil() -> str:
    import shutil

    path = shutil.which("certutil.exe") or shutil.which("certutil")
    if not path:
        raise RuntimeError("Windows certutil.exe was not found")
    return path



def _run_certutil(args: list[str], *, timeout: float = _CERTUTIL_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Run certutil without a shell and with a bounded lifetime.

    The return value is intentionally a small, redaction-safe operation record;
    callers must still decide whether the requested state transition happened.
    """
    import subprocess

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        result = subprocess.run(
            [_certutil(), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=flags,
            check=False,
            shell=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "timed_out": True,
            "message": "certutil 操作超时，未确认当前用户证书存储状态",
            "stdout": _bounded_process_output(exc.stdout),
            "stderr": _bounded_process_output(exc.stderr),
        }
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": result.stdout[-512:],
        "stderr": result.stderr[-512:],
    }



def _bounded_process_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return str(value)[-512:]



def install_current_user_root(cert_path: Path, owner_hwnd: int = 0) -> dict[str, Any]:
    del owner_hwnd  # certutil has no HWND parameter; consent is handled above this boundary.
    if os.name != "nt":
        raise RuntimeError("current-user root installation is Windows-only")
    if not cert_path.is_file():
        raise FileNotFoundError(cert_path)
    return _run_certutil(["-user", "-addstore", "Root", str(cert_path)])



def check_current_user_root(cert_path: Path) -> bool:
    """Check one exact certificate without going through ``inspect`` again.

    ``inspect`` delegates to this function by default, so constructing another
    manager here would recurse indefinitely.  The Windows store accepts a
    SHA-1 certificate thumbprint as its precise CertId; the UI still displays
    the SHA-256 fingerprint calculated by :class:`WindowsCertificateManager`.
    """
    if os.name != "nt" or not cert_path.is_file():
        return False
    thumbprint = WindowsCertificateManager._sha1_fingerprint(cert_path)
    return bool(
        thumbprint
        and _run_certutil(["-user", "-store", "Root", thumbprint]).get("ok", False)
    )



def remove_current_user_root(cert_path: Path, owner_hwnd: int = 0) -> dict[str, Any]:
    del owner_hwnd  # certutil has no HWND parameter; consent is handled above this boundary.
    if os.name != "nt":
        raise RuntimeError("current-user root removal is Windows-only")
    if not cert_path.is_file():
        return {"ok": True, "state": "missing", "message": "证书文件已不存在"}
    thumbprint = WindowsCertificateManager._sha1_fingerprint(cert_path)
    if not thumbprint:
        return {"ok": False, "state": "invalid", "message": "无法计算证书 SHA-1 指纹"}
    return _run_certutil(["-user", "-delstore", "Root", thumbprint])


class WindowsCertificateManager:
    """Inspect a local capture CA and gate all trust operations on consent."""

    _CANDIDATE_NAMES = (
        "mitmproxy-ca-cert.cer",
        "mitmproxy-ca-cert.pem",
        "mitmproxy-ca-cert.crt",
        "mitmproxy-ca-cert.der",
    )
    _PEM_RE = re.compile(
        rb"-----BEGIN CERTIFICATE-----\s*(.*?)\s*-----END CERTIFICATE-----",
        re.DOTALL,
    )

    def __init__(
        self,
        cert_dir: Path | str,
        *,
        certificate_path: Path | str | None = None,
        trust_checker: CertificateChecker | None = None,
        provisioner: CertificateProvisioner | None = None,
        remover: CertificateRemover | None = None,
        platform_name: str | None = None,
    ) -> None:
        self.cert_dir = Path(cert_dir)
        self._certificate_path = Path(certificate_path) if certificate_path is not None else None
        self._trust_checker = trust_checker or check_current_user_root
        self._provisioner = provisioner or install_current_user_root
        self._remover = remover or remove_current_user_root
        self._platform_name = platform_name or os.name

    @property
    def available(self) -> bool:
        return (
            self._platform_name == "nt"
            or self._trust_checker is not None
            or self._provisioner is not None
        )

    def inspect(self) -> CertificateStatus:
        path = self._find_certificate()
        if path is None:
            return CertificateStatus(
                state="missing", message="未找到采集 CA 证书文件"
            )
        fingerprint = self._fingerprint(path)
        if self._trust_checker is None:
            return CertificateStatus(
                state="unknown",
                fingerprint=fingerprint,
                path=str(path),
                message="证书文件存在，但尚未验证信任状态",
            )
        try:
            checked = self._trust_checker(path)
            if isinstance(checked, CertificateStatus):
                return CertificateStatus(
                    state=checked.state,
                    fingerprint=checked.fingerprint or fingerprint,
                    path=checked.path or str(path),
                    message=checked.message,
                )
            if isinstance(checked, str):
                state = checked
            else:
                state = "trusted" if checked else "untrusted"
            return CertificateStatus(
                state=state,
                fingerprint=fingerprint,
                path=str(path),
                message="已完成证书信任检查",
            )
        except Exception as exc:
            return CertificateStatus(
                state="error",
                fingerprint=fingerprint,
                path=str(path),
                message=f"证书信任检查失败：{type(exc).__name__}",
            )

    def prepare(self, consent: bool, owner_hwnd: int = 0) -> dict[str, Any]:
        before = self.inspect()
        if not consent:
            return self._result(
                ok=False,
                state="consent_required",
                status=before,
                message="未获得明确的证书安装同意，未写入任何证书或信任存储",
            )
        if before.state.lower() in {"trusted", "ready", "installed"}:
            return self._result(
                ok=True,
                state="already_trusted",
                status=before,
                message="采集 CA 已受信任，无需重复安装",
            )
        if before.state.lower() == "missing":
            return self._result(
                ok=False,
                state="missing",
                status=before,
                message="采集 CA 证书文件不存在，无法安装",
            )
        if self._provisioner is None:
            return self._result(
                ok=False,
                state="unsupported",
                status=before,
                message="未配置证书安装器，未修改任何证书",
            )
        path = Path(before.path)
        try:
            operation = self._provisioner(path, int(owner_hwnd))
        except Exception as exc:
            return self._result(
                ok=False,
                state="install_failed",
                status=before,
                message=f"证书安装失败：{type(exc).__name__}",
            )
        after = self.inspect()
        operation_ok = self._operation_succeeded(operation)
        if operation_ok and after.state.lower() in {"trusted", "ready", "installed"}:
            result = self._result(
                ok=True,
                state=after.state,
                status=after,
                message="采集 CA 已安装并获得信任",
            )
        else:
            result = self._result(
                ok=False,
                state="install_failed",
                status=after,
                message=(
                    "证书安装操作失败，未建立信任"
                    if not operation_ok
                    else "证书安装操作已返回，但当前用户 Root 中仍未信任该证书"
                ),
            )
        self._attach_operation(result, operation)
        return result

    def remove(self, consent: bool, owner_hwnd: int = 0) -> dict[str, Any]:
        before = self.inspect()
        if not consent:
            return self._result(
                ok=False,
                state="consent_required",
                status=before,
                message="未获得明确的证书移除同意，未写入任何证书或信任存储",
            )
        if before.state.lower() == "missing":
            return self._result(
                ok=True,
                state="already_absent",
                status=before,
                message="采集 CA 已不存在，无需移除",
            )
        if self._remover is None:
            return self._result(
                ok=False,
                state="unsupported",
                status=before,
                message="未配置证书移除器，未修改任何证书",
            )
        try:
            operation = self._remover(Path(before.path), int(owner_hwnd))
        except Exception as exc:
            return self._result(
                ok=False,
                state="remove_failed",
                status=before,
                message=f"证书移除失败：{type(exc).__name__}",
            )
        after = self.inspect()
        operation_ok = self._operation_succeeded(operation)
        after_state = after.state.lower()
        if operation_ok and after_state in {"missing", "untrusted"}:
            # The mitmproxy CA file is material, not trust state.  A successful
            # store removal therefore remains successful when that file stays on disk.
            result = self._result(
                ok=True,
                state="removed",
                status=after,
                message=(
                    "采集 CA 已从当前用户 Root 信任存储移除"
                    if after_state == "untrusted"
                    else "采集 CA 已移除"
                ),
            )
        else:
            result = self._result(
                ok=False,
                state="remove_failed",
                status=after,
                message=(
                    "证书移除操作失败，未确认信任存储已更改"
                    if not operation_ok
                    else "证书文件仍处于受信任状态，移除未完成"
                ),
            )
        self._attach_operation(result, operation)
        return result

    def consent_request(self, owner_hwnd: int = 0) -> CertificateConsentRequest:
        status = self.inspect()
        return CertificateConsentRequest(
            purpose="Trust the local Guance capture CA for HTTPS inspection",
            subject="mitmproxy local capture CA",
            fingerprint=status.fingerprint,
            owner_hwnd=owner_hwnd,
        )

    def _find_certificate(self) -> Path | None:
        if self._certificate_path is not None:
            return self._certificate_path if self._certificate_path.is_file() else None
        for name in self._CANDIDATE_NAMES:
            candidate = self.cert_dir / name
            if candidate.is_file():
                return candidate
        return None

    @classmethod
    def _certificate_bytes(cls, path: Path) -> bytes:
        content = path.read_bytes()
        match = cls._PEM_RE.search(content)
        if not match:
            return content
        try:
            return base64.b64decode(b"".join(match.group(1).split()), validate=True)
        except (ValueError, binascii.Error):
            return content

    @classmethod
    def _fingerprint(cls, path: Path) -> str:
        try:
            return hashlib.sha256(cls._certificate_bytes(path)).hexdigest().upper()
        except OSError:
            return ""

    @classmethod
    def _sha1_fingerprint(cls, path: Path) -> str:
        try:
            return hashlib.sha1(cls._certificate_bytes(path)).hexdigest().upper()
        except OSError:
            return ""

    @staticmethod
    def _operation_succeeded(operation: Any) -> bool:
        if isinstance(operation, Mapping):
            return bool(operation.get("ok", False))
        if isinstance(operation, bool):
            return operation
        return True

    @staticmethod
    def _attach_operation(result: dict[str, Any], operation: Any) -> None:
        if isinstance(operation, Mapping):
            result["operation"] = redact_value(
                {str(k): operation[k] for k in list(operation)[:16]}
            )

    @staticmethod
    def _result(*, ok: bool, state: str, status: CertificateStatus, message: str) -> dict[str, Any]:
        return {"ok": ok, "state": state, "certificate": status.to_dict(), "message": message}


__all__ = [
    "CertificateChecker",
    "CertificateProvisioner",
    "CertificateRemover",
    "WindowsCertificateManager",
    "check_current_user_root",
    "install_current_user_root",
    "remove_current_user_root",
]
