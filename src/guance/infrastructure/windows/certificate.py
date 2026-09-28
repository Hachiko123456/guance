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
from guance.ports.capture import (
    CertificateConsentRequest,
    CertificateStatus,
)


CertificateChecker = Callable[[Path], bool | str | CertificateStatus]
CertificateProvisioner = Callable[[Path, int], Any]
CertificateRemover = Callable[[Path, int], Any]


def _certutil() -> str:
    import shutil
    path = shutil.which("certutil.exe") or shutil.which("certutil")
    if not path:
        raise RuntimeError("Windows certutil.exe was not found")
    return path


def _run_certutil(args: list[str]) -> dict[str, Any]:
    import subprocess
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    result = subprocess.run([_certutil(), *args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", creationflags=flags, check=False)
    return {"ok": result.returncode == 0, "returncode": result.returncode,
            "stdout": result.stdout[-512:], "stderr": result.stderr[-512:]}


def install_current_user_root(cert_path: Path, owner_hwnd: int = 0) -> dict[str, Any]:
    if os.name != "nt":
        raise RuntimeError("current-user root installation is Windows-only")
    if not cert_path.is_file():
        raise FileNotFoundError(cert_path)
    return _run_certutil(["-user", "-addstore", "Root", str(cert_path)])


def check_current_user_root(cert_path: Path) -> bool:
    if os.name != "nt" or not cert_path.is_file():
        return False
    fingerprint = WindowsCertificateManager(cert_path.parent, certificate_path=cert_path).inspect().fingerprint
    return bool(fingerprint and _run_certutil(["-user", "-store", "Root", fingerprint])["ok"])


def remove_current_user_root(cert_path: Path, owner_hwnd: int = 0) -> dict[str, Any]:
    if os.name != "nt":
        raise RuntimeError("current-user root removal is Windows-only")
    fingerprint = WindowsCertificateManager(cert_path.parent, certificate_path=cert_path).inspect().fingerprint
    if not fingerprint:
        return {"ok": True, "state": "missing"}
    return _run_certutil(["-user", "-delstore", "Root", fingerprint])


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
                state="missing", message="capture CA certificate is not present"
            )
        fingerprint = self._fingerprint(path)
        if self._trust_checker is None:
            return CertificateStatus(
                state="unknown",
                fingerprint=fingerprint,
                path=str(path),
                message="certificate is present but trust has not been verified",
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
                message="certificate trust check completed",
            )
        except Exception as exc:
            return CertificateStatus(
                state="error",
                fingerprint=fingerprint,
                path=str(path),
                message=f"certificate trust check failed: {type(exc).__name__}",
            )

    def prepare(self, consent: bool, owner_hwnd: int = 0) -> dict[str, Any]:
        before = self.inspect()
        if not consent:
            return self._result(
                ok=False,
                state="consent_required",
                status=before,
                message="explicit certificate consent was not provided",
            )
        if before.state.lower() in {"trusted", "ready", "installed"}:
            return self._result(
                ok=True,
                state="already_trusted",
                status=before,
                message="capture CA is already trusted",
            )
        if before.state.lower() == "missing":
            return self._result(
                ok=False,
                state="missing",
                status=before,
                message="capture CA certificate is missing",
            )
        if self._provisioner is None:
            return self._result(
                ok=False,
                state="unsupported",
                status=before,
                message="no certificate provisioner is configured; no certificate was changed",
            )
        path = Path(before.path)
        try:
            operation = self._provisioner(path, int(owner_hwnd))
        except Exception as exc:
            return self._result(
                ok=False,
                state="error",
                status=before,
                message=f"certificate preparation failed: {type(exc).__name__}",
            )
        after = self.inspect()
        result = self._result(
            ok=after.state.lower() in {"trusted", "ready", "installed"},
            state=after.state,
            status=after,
            message=after.message or "certificate preparation completed",
        )
        if isinstance(operation, Mapping):
            result["operation"] = redact_value(
                {str(k): operation[k] for k in list(operation)[:16]}
            )
        return result

    def remove(self, consent: bool, owner_hwnd: int = 0) -> dict[str, Any]:
        before = self.inspect()
        if not consent:
            return self._result(
                ok=False,
                state="consent_required",
                status=before,
                message="explicit certificate removal consent was not provided",
            )
        if before.state.lower() == "missing":
            return self._result(
                ok=True,
                state="already_absent",
                status=before,
                message="capture CA is already absent",
            )
        if self._remover is None:
            return self._result(
                ok=False,
                state="unsupported",
                status=before,
                message="no certificate remover is configured; no certificate was changed",
            )
        try:
            operation = self._remover(Path(before.path), int(owner_hwnd))
        except Exception as exc:
            return self._result(
                ok=False,
                state="error",
                status=before,
                message=f"certificate removal failed: {type(exc).__name__}",
            )
        after = self.inspect()
        result = self._result(
            ok=after.state.lower() == "missing",
            state=after.state,
            status=after,
            message=after.message or "certificate removal completed",
        )
        if isinstance(operation, Mapping):
            result["operation"] = redact_value(
                {str(k): operation[k] for k in list(operation)[:16]}
            )
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
    def _fingerprint(cls, path: Path) -> str:
        try:
            content = path.read_bytes()
        except OSError:
            return ""
        match = cls._PEM_RE.search(content)
        if match:
            try:
                content = base64.b64decode(b"".join(match.group(1).split()), validate=True)
            except (ValueError, binascii.Error):
                pass
        return hashlib.sha256(content).hexdigest().upper()

    @staticmethod
    def _result(*, ok: bool, state: str, status: CertificateStatus, message: str) -> dict[str, Any]:
        return {"ok": ok, "state": state, "certificate": status.to_dict(), "message": message}


__all__ = [
    "CertificateChecker",
    "CertificateProvisioner",
    "CertificateRemover",
    "WindowsCertificateManager",
]
