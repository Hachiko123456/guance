"""Offline-delivery acceptance for the fresh no-capture onedir package.

Subcommands are ordered and evidence-producing only:

* ``baseline``  - git/read-only baseline, git readiness inputs;
* ``docs``      - public documentation link/consistency check;
* ``audit``     - onedir inventory, version, CPython closure, static audit;
* ``smoke``     - real no-network frozen GUI smoke in a temp data dir;
* ``worker``    - frozen FableDan load/recommend/close/stop + MARVEL fail-closed;
* ``finalize``  - build identity, per-file SHA256SUMS, onedir ZIP + SHA-256.

Nothing here launches capture, changes proxy/certificate state, trains, or touches
the accepted CPU/GPU environments. All writable runtime data goes to a temporary
directory; only bounded JSON/log evidence is written under the task evidence root.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
import zipfile

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "build" / "acceptance" / "guance-offline-delivery-20261020"
PACKAGE = EVIDENCE / "package" / "dist" / "Guance"
BUILD_REPORTS = EVIDENCE / "package" / "reports"
PROTOCOL_VERSION = 3
IDENTITY_FIELDS = ("request_id", "session_id", "game_id", "state_version", "epoch", "generation")
CPU_PYTHON = Path(sys.executable)

PUBLIC_DOCS = (
    "README.md", "docs/BUILD.md", "docs/RELEASE.md", "docs/USER_GUIDE.md",
    "docs/MODEL_BACKENDS.md", "docs/GPU_INFERENCE.md", "docs/PROJECT_STATUS.md",
    "docs/OFFLINE_DELIVERY.md", "docs/THIRD_PARTY_AND_REDISTRIBUTION.md",
    "docs/ARCHITECTURE.md", "docs/SECURITY.md", "docs/PROTOCOL.md",
    "docs/DIAGNOSTICS.md", "docs/INTEGRATION_CONTRACT.md", "docs/ACCEPTANCE.md",
    "docs/ACCEPTANCE_STANDARD.md", "docs/UI_ACCEPTANCE.md",
)

VIEW = {
    "schema_version": 1, "ruleset_id": "tencent_daguandan.v1",
    "session_id": "delivery-session", "game_id": "delivery-game", "state_version": 1,
    "my_seat": 0, "current_seat": 0, "first_seat": 0, "partner_seat": 2,
    "level": 2, "phase": "play", "trick": 0, "hand": [17, 19],
    "remaining": [2, 27, 27, 27], "public_history": [], "lead_action": None,
    "free_lead": True, "visibility": "public_plus_self", "completeness": "public_play",
    "ambiguities": [],
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def git(*args: str) -> str:
    result = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed")
    return result.stdout.decode("utf-8", errors="replace")


def inventory(root: Path) -> list[dict[str, Any]]:
    return [
        {"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size,
         "sha256": sha256(path)}
        for path in sorted((p for p in root.rglob("*") if p.is_file()), key=lambda p: p.as_posix())
    ]


# ---------------------------------------------------------------- baseline
def cmd_baseline(_: argparse.Namespace) -> int:
    out = EVIDENCE / "baseline"
    out.mkdir(parents=True, exist_ok=True)
    status = git("status", "--porcelain=v1")
    (out / "git-status.txt").write_text(status, encoding="utf-8")
    tracked = [item for item in git("ls-files", "-z").split("\x00") if item]
    untracked = [item for item in git("ls-files", "--others", "--exclude-standard", "-z").split("\x00") if item]
    ignored = [line[3:] for line in git("status", "--porcelain=v1", "--ignored").splitlines()
               if line.startswith("!! ")]
    payload = {
        "schema": "guance-offline-delivery-baseline-v1",
        "recorded_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "head": git("rev-parse", "HEAD").strip(),
        "branch": git("branch", "--show-current").strip(),
        "upstream": git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}").strip(),
        "upstream_ahead_behind": git("rev-list", "--left-right", "--count",
                                     "HEAD...@{u}").strip(),
        "remote_freshness": "UNVERIFIED (no network fetch performed)",
        "refs": [line.split() for line in git(
            "for-each-ref", "--format=%(refname) %(objectname)",
            "refs/heads", "refs/remotes").splitlines()],
        "tracked_count": len(tracked),
        "untracked_count": len(untracked),
        "ignored_top_level": sorted({item.split("/")[0] for item in ignored}),
        "modified_tracked": [line[3:] for line in status.splitlines() if line.startswith(" M")],
        "staged": [line[3:] for line in status.splitlines() if line[:2].strip() and line[0] in "MADRCTU"],
        "untracked": untracked,
        "tracked_diff_sha256": hashlib.sha256(git("diff", "--binary", "HEAD").encode("utf-8")).hexdigest(),
        "user_changes_attribution": "pre-existing dirty tree preserved; not authored by this task",
    }
    write_json(out / "baseline-audit.json", payload)
    readiness = {
        "schema": "guance-offline-delivery-git-readiness-v1",
        "head": payload["head"], "branch": payload["branch"], "upstream": payload["upstream"],
        "upstream_ahead_behind": payload["upstream_ahead_behind"],
        "remote_backup_claim": False, "remote_freshness": payload["remote_freshness"],
        "staged_count": len(payload["staged"]),
        "modified_tracked_count": len(payload["modified_tracked"]),
        "untracked_count": payload["untracked_count"],
        "current_tree_fully_committed": False,
        "statement": "The current candidate is NOT fully committed; required untracked source files exist only in the working tree.",
        "git_operations_performed": "none (read-only)",
        "status": "GIT_COMMIT_PENDING_USER_AUTHORIZATION",
    }
    write_json(out / "git-readiness.json", readiness)
    print(json.dumps({"head": payload["head"], "tracked": len(tracked),
                      "untracked": len(untracked)}, sort_keys=True))
    return 0


# ---------------------------------------------------------------- docs
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)


def _slug(text: str) -> str:
    value = re.sub(r"[^\w一-鿿 -]", "", text.strip().lower())
    return value.replace(" ", "-")


def cmd_docs(_: argparse.Namespace) -> int:
    out = EVIDENCE / "docs"
    out.mkdir(parents=True, exist_ok=True)
    broken: list[dict[str, str]] = []
    checked = 0
    anchors_cache: dict[Path, set[str]] = {}

    def anchors(path: Path) -> set[str]:
        if path not in anchors_cache:
            text = path.read_text(encoding="utf-8")
            anchors_cache[path] = {_slug(m.group(1)) for m in HEADING_RE.finditer(text)}
        return anchors_cache[path]

    for relative in PUBLIC_DOCS:
        path = ROOT / relative
        if not path.is_file():
            broken.append({"source": relative, "target": relative, "reason": "missing public document"})
            continue
        text = path.read_text(encoding="utf-8")
        for match in LINK_RE.finditer(text):
            target = match.group(1).strip().split(" ")[0]
            if not target or target.startswith(("http://", "https://", "mailto:", "#")):
                if target.startswith("#") and _slug(target[1:]) not in anchors(path):
                    broken.append({"source": relative, "target": target, "reason": "missing anchor"})
                continue
            checked += 1
            clean = target.split("#", 1)[0]
            resolved = (path.parent / clean).resolve()
            if not resolved.exists():
                broken.append({"source": relative, "target": target, "reason": "missing target"})
                continue
            if "#" in target and resolved.suffix == ".md":
                if _slug(target.split("#", 1)[1]) not in anchors(resolved):
                    broken.append({"source": relative, "target": target, "reason": "missing anchor"})
    corpus = "\n".join((ROOT / item).read_text(encoding="utf-8")
                       for item in PUBLIC_DOCS if (ROOT / item).is_file())
    required_phrases = {
        "phase7_paused": "PAUSED" in corpus and "NOT AUTHORIZED" in corpus.upper(),
        "phase6_counts": "9 ready" in corpus and "11 fail_closed" in corpus,
        "production_ready_false": "production_ready=false" in corpus,
        "strength_claim_false": "strength_claim=false" in corpus,
        "capture_disabled": "CAPTURE_DISABLED_IN_EVALUATION_PACKAGE" in corpus or "no-capture" in corpus,
        "redistribution_not_authorized": "REDISTRIBUTION_NOT_AUTHORIZED" in corpus,
        "frozen_gpu_unverified": "FROZEN_GPU_UNVERIFIED" in corpus or "NOT_IMPLEMENTED" in corpus,
    }
    forbidden_phrases = {
        "old_candidate_9": "Candidate 9",
        "packaged_gpu_claim": "GPU 已打包",
        "all_packaged_claim": "全部打包完成",
    }
    present_forbidden = {name: corpus.count(value) for name, value in forbidden_phrases.items()
                         if value in corpus}
    report = {
        "schema": "guance-offline-delivery-docs-check-v1",
        "public_docs": list(PUBLIC_DOCS),
        "links_checked": checked,
        "broken_links": broken,
        "required_phrases": required_phrases,
        "forbidden_phrases_present": present_forbidden,
        "status": "passed" if (not broken and all(required_phrases.values())
                               and not present_forbidden) else "failed",
    }
    write_json(out / "docs-link-check.json", report)
    print(json.dumps({"links": checked, "broken": len(broken),
                      "status": report["status"]}, sort_keys=True))
    return 0 if report["status"] == "passed" else 2


# ---------------------------------------------------------------- audit
STATIC_AUDIT = ROOT / "scripts" / "run_phase8_package_acceptance.py"


def _python_dlls(package: Path) -> list[str]:
    return sorted(path.name for path in (package / "_internal").rglob("python3*.dll"))


def cmd_audit(args: argparse.Namespace) -> int:
    package = args.package.resolve()
    if not (package / "Guance.exe").is_file():
        raise SystemExit(f"missing package: {package}")
    BUILD_REPORTS.mkdir(parents=True, exist_ok=True)
    audit_env = os.environ.copy()
    audit_env["PYTHONDONTWRITEBYTECODE"] = "1"
    audit = subprocess.run(
        [str(CPU_PYTHON), "-B", str(STATIC_AUDIT), "--package-root", str(package),
         "--evidence-root", str(EVIDENCE / "package"), "--candidate", "8"],
        cwd=ROOT, env=audit_env, capture_output=True, text=True, check=False,
    )
    (BUILD_REPORTS / "static-audit.log").write_text(
        f"exit={audit.returncode}\n{audit.stdout}\n{audit.stderr}\n", encoding="utf-8")
    version = subprocess.run([str(package / "Guance.exe"), "--version"],
                             capture_output=True, text=True, timeout=60, check=False)
    files = inventory(package)
    dlls = _python_dlls(package)
    names = {Path(item["path"]).name.lower() for item in files}
    forbidden_any = {
        "capture_helper": any(name == "mitmdump.exe" for name in names),
        "python314": "python314.dll" in names,
        "torch": any(item["path"].startswith("_internal/torch/") for item in files),
        "checkpoint": any(Path(item["path"]).suffix.lower() in {".pt", ".pth", ".ckpt"}
                          for item in files),
        "runtime_dir": (package / "_internal" / "runtime").exists(),
    }
    closure = {
        "python312_dll": "python312.dll" in dlls,
        "python_dlls": dlls,
        "exe_version_stdout": version.stdout.strip(),
        "exe_version_exit": version.returncode,
    }
    report = {
        "schema": "guance-offline-delivery-static-audit-v1",
        "package_root": str(package),
        "file_count": len(files),
        "tree_bytes": sum(item["bytes"] for item in files),
        "static_audit_exit": audit.returncode,
        "static_audit_summary": json.loads((EVIDENCE / "package" / "reports"
                                            / "package-audit-summary.json").read_text(encoding="utf-8"))
        if (EVIDENCE / "package" / "reports" / "package-audit-summary.json").is_file() else None,
        "closure": closure,
        "forbidden_present": forbidden_any,
        "status": "passed" if (
            audit.returncode == 0 and closure["python312_dll"]
            and version.returncode == 0 and not any(forbidden_any.values())
        ) else "failed",
    }
    write_json(BUILD_REPORTS / "static-audit.json", report)
    print(json.dumps({"files": len(files), "status": report["status"]}, sort_keys=True))
    return 0 if report["status"] == "passed" else 2


# ---------------------------------------------------------------- smoke
def cmd_smoke(args: argparse.Namespace) -> int:
    package = args.package.resolve()
    out = EVIDENCE / "package" / "smoke"
    out.mkdir(parents=True, exist_ok=True)
    data_dir = Path(tempfile.mkdtemp(prefix="guance-frozen-smoke-data-"))
    env = os.environ.copy()
    env["GUANCE_DATA_DIR"] = str(data_dir)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    sessions: list[dict[str, Any]] = []
    for name, state, theme in (("default-light", "default", "light"),
                               ("unavailable-dark", "unavailable", "dark"),
                               ("error-light", "error", "light")):
        report = out / f"gui-smoke-{name}.json"
        screenshot = out / f"gui-smoke-{name}.png"
        command = [str(package / "Guance.exe"), "--frozen-smoke",
                   "--smoke-state", state, "--smoke-theme", theme,
                   "--smoke-report", str(report), "--screenshot", str(screenshot),
                   "--exit-after", "8"]
        result = subprocess.run(command, env=env, capture_output=True, text=True,
                                timeout=60, check=False)
        payload = json.loads(report.read_text(encoding="utf-8")) if report.is_file() else None
        sessions.append({
            "name": name, "command": command, "exit": result.returncode,
            "report": str(report), "screenshot": str(screenshot),
            "screenshot_bytes": screenshot.stat().st_size if screenshot.is_file() else 0,
            "report_status": payload.get("status") if payload else None,
            "frozen": payload.get("frozen") if payload else None,
            "no_network_mode": payload.get("no_network_mode") if payload else None,
            "controller_created": payload.get("controller_created") if payload else None,
            "proxy_or_certificate_operations": (
                payload.get("proxy_or_certificate_operations") if payload else None),
            "capture_capability": payload.get("capture_capability") if payload else None,
            "module_guards": payload.get("module_guards") if payload else None,
            "root_visible": payload.get("root_visible") if payload else None,
            "qt_messages_error_count": sum(
                1 for item in (payload.get("qt_messages") or []) if item.get("type") in (2, 3)
            ) if payload else None,
        })
    ok = all(
        item["exit"] == 0 and item["report_status"] == "passed"
        and item["frozen"] is True and item["no_network_mode"] is True
        and item["controller_created"] is False
        and item["proxy_or_certificate_operations"] is False
        and item["root_visible"] is True
        and not any((item["module_guards"] or {}).values())
        for item in sessions
    )
    summary = {
        "schema": "guance-offline-delivery-frozen-smoke-v1",
        "isolated_data_dir": str(data_dir),
        "sessions": sessions,
        "status": "passed" if ok else "failed",
    }
    write_json(out / "gui-smoke-summary.json", summary)
    print(json.dumps({"sessions": len(sessions), "status": summary["status"]}, sort_keys=True))
    return 0 if ok else 2


# ---------------------------------------------------------------- worker
def _envelope(kind: str, request_id: str, **body: Any) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION, "type": kind,
        "request_id": request_id, "session_id": "delivery-session",
        "game_id": "delivery-game", "state_version": 1, "epoch": 0, "generation": 0,
        **body,
    }


class WorkerSession:
    def __init__(self, exe: Path, env: dict[str, str]) -> None:
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.process = subprocess.Popen(
            [str(exe), "--model-worker", "--threads", "2", "--device", "auto"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1, env=env,
        )
        self.stdout_raw: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.stdout_raw.append(line)
            self.lines.put(line)
        self.lines.put(None)

    def send(self, payload: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        self.process.stdin.flush()

    def reply(self, timeout: float) -> dict[str, Any] | None:
        line = self.lines.get(timeout=timeout)
        return json.loads(line) if line else None

    def close(self, graceful: bool = True) -> dict[str, Any]:
        if graceful:
            self.send(_envelope("close", "delivery-close"))
            try:
                self.reply(5.0)
                self.process.stdin.close()  # type: ignore[union-attr]
            except Exception:
                pass
        try:
            exit_code = self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            exit_code = self.process.wait(timeout=5)
        stderr = self.process.stderr.read() if self.process.stderr else ""
        return {"exit": exit_code, "stderr": stderr[-2000:]}


def _npz_source(package: Path) -> Path:
    return package / "_internal" / "data" / "profiles" / "tencent_daguandan" / "models" / "best.npz"


def _run_fabledan_session(package: Path, env: dict[str, str], *, stop: bool) -> dict[str, Any]:
    source = _npz_source(package)
    session = WorkerSession(package / "Guance.exe", env)
    loaded: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    terminal: dict[str, Any] | None = None
    try:
        session.send(_envelope("load", "delivery-load", source=str(source)))
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            reply = session.reply(max(0.1, deadline - time.monotonic()))
            if reply is None:
                break
            if reply.get("type") == "loaded":
                loaded = reply.get("metadata")
                break
            if reply.get("type") == "error":
                return {"status": "failed", "code": reply.get("error_code"),
                        "loaded": None, "final": session.close(graceful=False)}
        if loaded is None:
            return {"status": "failed", "code": "NO_LOADED_REPLY",
                    "final": session.close(graceful=False)}
        session.send(_envelope("recommend", "delivery-recommend", request_kind="public",
                               observation=VIEW))
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            reply = session.reply(max(0.1, deadline - time.monotonic()))
            if reply is None:
                break
            if reply.get("type") == "result":
                result = reply.get("result")
                break
            if reply.get("type") == "error":
                return {"status": "failed", "code": reply.get("error_code"),
                        "loaded": loaded, "final": session.close(graceful=False)}
        if result is None:
            return {"status": "failed", "code": "NO_RESULT_REPLY", "loaded": loaded,
                    "final": session.close(graceful=False)}
        terminal_kind = "stop" if stop else "close"
        session.send(_envelope(terminal_kind, "delivery-terminal"))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            reply = session.reply(max(0.1, deadline - time.monotonic()))
            if reply is None:
                break
            if reply.get("type") == "status":
                terminal = reply
                break
        final = session.close(graceful=False)
        good = (
            loaded.get("provider") == "fabledan"
            and result.get("status") == "ready"
            and len(result.get("candidates") or []) >= 1
            and result.get("model_fingerprint") == sha256(source)
            and terminal is not None
            and terminal.get("model_status") == ("stopped" if stop else "closed")
            and final["exit"] == 0
        )
        return {
            "status": "passed" if good else "failed",
            "loaded": {key: loaded.get(key) for key in (
                "status", "provider", "backend", "runtime", "model_version",
                "model_sha256", "bundle_fingerprint", "score_kind", "test_only")},
            "recommendation": {
                "status": result.get("status"),
                "candidates": len(result.get("candidates") or []),
                "model_fingerprint": result.get("model_fingerprint"),
                "score_kind": result.get("score_kind"),
                "candidate_ranks": [item.get("rank") for item in result.get("candidates") or []],
                "cards_public": [item.get("cards") for item in result.get("candidates") or []],
            },
            "terminal_status": terminal.get("model_status") if terminal else None,
            "final": final,
        }
    finally:
        if session.process.poll() is None:
            session.process.kill()


def _run_marvel_failclosed(package: Path, env: dict[str, str]) -> dict[str, Any]:
    descriptor = (ROOT / "build" / "acceptance" / "guance-gpu-first-inference-20261020"
                  / "bundles" / "marvel-v2-iter002000-device")
    session = WorkerSession(package / "Guance.exe", env)
    replies: list[dict[str, Any]] = []
    try:
        session.send(_envelope("load", "delivery-marvel", source=str(descriptor)))
        deadline = time.monotonic() + 60
        error = None
        loaded = None
        while time.monotonic() < deadline:
            reply = session.reply(max(0.1, deadline - time.monotonic()))
            if reply is None:
                break
            replies.append({"type": reply.get("type"), "code": reply.get("error_code")})
            if reply.get("type") == "error":
                error = reply
                break
            if reply.get("type") == "loaded":
                loaded = reply
                break
        final = session.close(graceful=True)
        no_auto_fallback = all(item["type"] not in {"loaded", "result"} for item in replies)
        good = (error is not None and loaded is None and no_auto_fallback
                and final["exit"] == 0)
        return {
            "status": "passed" if good else "failed",
            "descriptor": str(descriptor),
            "replies": replies,
            "error_code": error.get("error_code") if error else None,
            "auto_loaded_fabledan": not no_auto_fallback,
            "recommendation_count": 0,
            "final": final,
        }
    finally:
        if session.process.poll() is None:
            session.process.kill()


def _run_missing_model(package: Path, env: dict[str, str]) -> dict[str, Any]:
    missing = package / "_internal" / "data" / "models" / "does-not-exist.npz"
    session = WorkerSession(package / "Guance.exe", env)
    try:
        session.send(_envelope("load", "delivery-missing", source=str(missing)))
        reply = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            reply = session.reply(max(0.1, deadline - time.monotonic()))
            if reply is None:
                break
            if reply.get("type") in {"error", "loaded"}:
                break
        final = session.close(graceful=True)
        good = (reply is not None and reply.get("type") == "error"
                and reply.get("error_code") in {"MODEL_LOAD_FAILED", "MODEL_SOURCE_INVALID",
                                                "MODEL_UNAVAILABLE", "ARTIFACT_MISSING",
                                                "MANIFEST_MISSING"})
        return {
            "status": "passed" if good else "failed",
            "requested": str(missing), "reply_type": reply.get("type") if reply else None,
            "error_code": reply.get("error_code") if reply else None,
            "recommendation_count": 0, "final": final,
        }
    finally:
        if session.process.poll() is None:
            session.process.kill()


def cmd_worker(args: argparse.Namespace) -> int:
    package = args.package.resolve()
    out = EVIDENCE / "package" / "smoke"
    out.mkdir(parents=True, exist_ok=True)
    data_dir = Path(tempfile.mkdtemp(prefix="guance-frozen-worker-data-"))
    env = os.environ.copy()
    env["GUANCE_DATA_DIR"] = str(data_dir)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["GUANCE_MODEL_DEVICE"] = "auto"
    # The production client always launches the worker with an explicit UTF-8
    # stdio contract; reproduce it so non-ASCII candidate labels do not corrupt
    # the JSONL stream on a GBK Windows console.
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
               PYTHONNOUSERSITE="1", PYTHONSAFEPATH="1")
    for name in ("GUANCE_MARVEL_V2_SOURCE", "GUANCE_MARVEL_V2_CHECKPOINT",
                 "GUANCE_MODEL_BUNDLE", "GUANCE_MODEL_PATH"):
        env.pop(name, None)
    fabledan_close = _run_fabledan_session(package, env, stop=False)
    fabledan_stop = _run_fabledan_session(package, env, stop=True)
    marvel_env = dict(env)
    marvel_env["GUANCE_MARVEL_V2_SOURCE"] = str(
        ROOT / "build" / "acceptance" / "guance-model-platform-20261016" / "phase2"
        / "runtime" / "marvel-e5c82e7c-d7acbc8d" / "source")
    marvel_env["GUANCE_MARVEL_V2_CHECKPOINT"] = (
        r"D:\project\guandan\runs\comparison_delivery\dataset\m09_candidate_iter002000.pt")
    marvel = _run_marvel_failclosed(package, marvel_env)
    missing = _run_missing_model(package, env)
    summary = {
        "schema": "guance-offline-delivery-frozen-worker-v1",
        "isolated_data_dir": str(data_dir),
        "fabledan_close": fabledan_close,
        "fabledan_stop": fabledan_stop,
        "marvel_fail_closed": marvel,
        "missing_model": missing,
        "status": "passed" if all(item["status"] == "passed" for item in
                                  (fabledan_close, fabledan_stop, marvel, missing)) else "failed",
    }
    write_json(out / "frozen-worker-summary.json", summary)
    print(json.dumps({key: summary[key]["status"] for key in
                      ("fabledan_close", "fabledan_stop", "marvel_fail_closed",
                       "missing_model")}, sort_keys=True))
    return 0 if summary["status"] == "passed" else 2


# ---------------------------------------------------------------- capability
def capability_entries(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the explicit capability matrix from measured facts only."""
    source = facts.get("source_check", {})
    package = facts.get("package", {})
    frozen_ok = all(package.get(name) == "passed" for name in
                    ("static_audit", "gui_smoke", "frozen_worker"))
    frozen = "PASSED" if frozen_ok else "UNVERIFIED"
    source_cpu = "PASSED" if source.get("cpu-only-auto") == "passed" else "UNVERIFIED"
    source_gpu = "PASSED" if source.get("gpu-auto") == "passed" else "UNVERIFIED"
    forced_cpu = "PASSED" if source.get("forced-cpu") == "passed" else "UNVERIFIED"
    cuda_fail = "PASSED" if source.get("cpu-only-fail-closed") == "passed" else "UNVERIFIED"
    hidden = source.get("no-cuda-hidden")
    hidden_status = "PASSED" if hidden == "passed" else "UNVERIFIED"
    return [
        {"capability": "source_cpu", "status": source_cpu,
         "detail": "Accepted CPU-only Torch 2.13.0+cpu source run: auto->cpu with explicit reason and a legal MARVEL recommendation.",
         "evidence": "source-check/cpu-only-auto.json"},
        {"capability": "source_gpu_auto", "status": source_gpu,
         "detail": "Isolated cu126 environment: auto selects cuda:0 with CUDA_PROBE_OK and produces legal MARVEL recommendations.",
         "evidence": "source-check/gpu-auto.json"},
        {"capability": "source_forced_cpu", "status": forced_cpu,
         "detail": "cu126 build forced to cpu: actual_device=cpu, torch_cuda_build=12.6, no fabricated CPU-only build.",
         "evidence": "source-check/forced-cpu.json"},
        {"capability": "source_cuda_fail_closed", "status": cuda_fail,
         "detail": "Accepted CPU-only Torch process: auto->cpu with CPU_ONLY_TORCH and a legal recommendation; forced cuda fails MODEL_UNAVAILABLE with zero recommendations and zero model fallback.",
         "evidence": "source-check/cpu-only-fail-closed.json"},
        {"capability": "source_hidden_device_fallback", "status": hidden_status,
         "detail": "Process-hidden CUDA device (CUDA_VISIBLE_DEVICES empty) currently classifies as MODEL_INFERENCE_FAILED instead of a no-device CPU fallback. This is a device-policy edge case reported as P1; the required CPU-only auto->CPU path is separately PASSED.",
         "evidence": "source-check/no-cuda-hidden.json"},
        {"capability": "fresh_frozen_no_capture", "status": frozen,
         "detail": "Fresh onedir built from the current source manifest; no-network GUI smoke and frozen FableDan load/recommend/close/stop pass.",
         "evidence": "package/build-identity.json, package/smoke/gui-smoke-summary.json"},
        {"capability": "frozen_fabledan", "status": "PASSED" if package.get("frozen_worker") == "passed" else "UNVERIFIED",
         "detail": "Frozen FableDan (numpy) loads the packaged best.npz, returns a ready recommendation, and closes/stops cleanly.",
         "evidence": "package/smoke/frozen-worker-summary.json"},
        {"capability": "frozen_marvel_gpu", "status": "NOT_IMPLEMENTED",
         "detail": "The no-capture package contains no Torch/MARVEL runtime or checkpoint; a MARVEL descriptor fails closed with no model fallback.",
         "evidence": "package/smoke/frozen-worker-summary.json"},
        {"capability": "frozen_gpu_self_contained", "status": "NOT_IMPLEMENTED",
         "detail": "A self-contained/portable frozen CUDA distribution was not developed or verified in this task.",
         "evidence": "docs/OFFLINE_DELIVERY.md"},
        {"capability": "capture", "status": "UNSUPPORTED",
         "detail": "Evaluation package excludes the capture helper; frozen capture status is unavailable and never falls back to a system mitmproxy.",
         "evidence": "package/smoke/gui-smoke-summary.json"},
        {"capability": "live_ui", "status": "UNVERIFIED",
         "detail": "Recommendation displayed/live input remain UNVERIFIED/false; the GUI smoke is a bounded no-network fixture, not live model display.",
         "evidence": "docs/PROJECT_STATUS.md"},
        {"capability": "cross_machine", "status": "UNVERIFIED",
         "detail": "Only this physical host was exercised; cross-machine portability, DPI and OS differences were not tested.",
         "evidence": "docs/OFFLINE_DELIVERY.md"},
        {"capability": "redistribution", "status": "NOT AUTHORIZED",
         "detail": "Local experimental/evaluation-only. Third-party notices and FableDan/Qt/CRT redistribution rights are not confirmed.",
         "evidence": "docs/THIRD_PARTY_AND_REDISTRIBUTION.md"},
    ]


def cmd_capability(args: argparse.Namespace) -> int:
    package = args.package.resolve()
    out = EVIDENCE / "docs"
    out.mkdir(parents=True, exist_ok=True)
    source_check: dict[str, Any] = {}
    for mode in ("gpu-auto", "forced-cpu", "no-cuda-hidden", "cpu-only-fail-closed",
                 "cpu-only-auto"):
        path = EVIDENCE / "source-check" / f"{mode}.json"
        source_check[mode] = (json.loads(path.read_text(encoding="utf-8")).get("status")
                              if path.is_file() else "missing")
    audit_path = BUILD_REPORTS / "static-audit.json"
    smoke_path = EVIDENCE / "package" / "smoke" / "gui-smoke-summary.json"
    worker_path = EVIDENCE / "package" / "smoke" / "frozen-worker-summary.json"
    package_facts = {
        "static_audit": (json.loads(audit_path.read_text(encoding="utf-8")).get("status")
                         if audit_path.is_file() else "missing"),
        "gui_smoke": (json.loads(smoke_path.read_text(encoding="utf-8")).get("status")
                      if smoke_path.is_file() else "missing"),
        "frozen_worker": (json.loads(worker_path.read_text(encoding="utf-8")).get("status")
                          if worker_path.is_file() else "missing"),
    }
    entries = capability_entries({"source_check": source_check, "package": package_facts})
    matrix = {
        "schema": "guance-offline-delivery-capability-matrix-v1",
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "package_root": str(package),
        "source_check": source_check,
        "package_facts": package_facts,
        "entries": entries,
        "boundaries": {
            "production_ready": False, "strength_claim": False,
            "recommendation_displayed": False, "live_input_used": False,
            "redistribution_authorized": False, "phase7_started": False,
        },
    }
    write_json(out / "capability-matrix.json", matrix)
    manifest = {
        "schema": "guance-package-capability-manifest-v1",
        "package_root": str(package),
        "capabilities": {entry["capability"]: entry["status"] for entry in entries},
        "production_ready": False, "strength_claim": False,
        "capture_enabled": False, "torch_included": False, "marvel_included": False,
        "gpu_included": False, "fabledan_included": True,
        "external_dependencies": "None for FableDan; MARVEL requires an external runtime, checkpoint and Torch environment.",
        "classification": "local experimental/evaluation-only",
    }
    write_json(EVIDENCE / "package" / "capability-manifest.json", manifest)
    status = "passed" if all(entry["status"] in {"PASSED", "UNVERIFIED", "UNSUPPORTED",
                                                 "NOT_IMPLEMENTED", "NOT AUTHORIZED"}
                             for entry in entries) else "failed"
    print(json.dumps({entry["capability"]: entry["status"] for entry in entries},
                     sort_keys=True))
    return 0 if status == "passed" else 2


# ---------------------------------------------------------------- finalize
def _package_versions(package: Path) -> dict[str, Any]:
    probe = (
        "import json,platform,sys,struct\n"
        "import PySide6,shiboken6,numpy\n"
        "import importlib.metadata as m\n"
        "from PySide6.QtCore import qVersion\n"
        "print(json.dumps({'python':platform.python_version(),"
        "'pointer_bits':struct.calcsize('P')*8,"
        "'pyside6':PySide6.__version__,'shiboken6':shiboken6.__version__,"
        "'qt':qVersion(),'numpy':numpy.__version__,"
        "'pyinstaller':m.version('PyInstaller'),'pytest':m.version('pytest')},sort_keys=True))\n"
    )
    result = subprocess.run([str(CPU_PYTHON), "-B", "-I", "-c", probe],
                            capture_output=True, text=True, check=False, timeout=120)
    return json.loads(result.stdout.strip().splitlines()[-1])


def cmd_finalize(args: argparse.Namespace) -> int:
    package = args.package.resolve()
    out = EVIDENCE / "package"
    out.mkdir(parents=True, exist_ok=True)
    files = inventory(package)
    sums_lines = [f"{item['sha256']}  {item['path']}" for item in files]
    (package / "SHA256SUMS.txt").write_text("\n".join(sums_lines) + "\n", encoding="utf-8")
    files_with_sums = inventory(package)
    (out / "SHA256SUMS.txt").write_text(
        "\n".join(f"{item['sha256']}  {item['path']}" for item in files_with_sums) + "\n",
        encoding="utf-8")
    zip_path = out / "Guance-offline-no-capture-onedir.zip"
    tmp_zip = zip_path.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for item in files_with_sums:
            archive.write(package / item["path"], arcname=f"Guance/{item['path']}")
    os.replace(tmp_zip, zip_path)
    audit = json.loads((BUILD_REPORTS / "static-audit.json").read_text(encoding="utf-8"))
    smoke = json.loads((out / "smoke" / "gui-smoke-summary.json").read_text(encoding="utf-8"))
    worker = json.loads((out / "smoke" / "frozen-worker-summary.json").read_text(encoding="utf-8"))
    identity = {
        "schema": "guance-offline-delivery-build-identity-v1",
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "git": {
            "head": git("rev-parse", "HEAD").strip(),
            "branch": git("branch", "--show-current").strip(),
            "dirty": bool(git("status", "--porcelain=v1").strip()),
            "tracked_diff_sha256": hashlib.sha256(
                git("diff", "--binary", "HEAD").encode("utf-8")).hexdigest(),
        },
        "build_command": [
            str(CPU_PYTHON), "-B", "scripts/build_release.py", "--exclude-capture-runtime",
            "--dist-dir", str(EVIDENCE / "package" / "dist"),
            "--work-dir", str(EVIDENCE / "package" / "work"),
        ],
        "build_environment": _package_versions(package),
        "package": {
            "root": str(package), "file_count": len(files_with_sums),
            "tree_bytes": sum(item["bytes"] for item in files_with_sums),
            "exe_sha256": sha256(package / "Guance.exe"),
            "exe_bytes": (package / "Guance.exe").stat().st_size,
            "exe_mtime": datetime.fromtimestamp(
                (package / "Guance.exe").stat().st_mtime).astimezone().isoformat(),
        },
        "zip": {"path": str(zip_path), "bytes": zip_path.stat().st_size,
                "sha256": sha256(zip_path),
                "created_at": datetime.now(timezone.utc).astimezone().isoformat()},
        "results": {"static_audit": audit["status"], "gui_smoke": smoke["status"],
                    "frozen_worker": worker["status"]},
        "status": "passed" if all(value == "passed" for value in (
            audit["status"], smoke["status"], worker["status"])) else "failed",
    }
    write_json(out / "build-identity.json", identity)
    print(json.dumps({"zip_sha256": identity["zip"]["sha256"],
                      "files": identity["package"]["file_count"],
                      "status": identity["status"]}, sort_keys=True))
    return 0 if identity["status"] == "passed" else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, function in (("baseline", cmd_baseline), ("docs", cmd_docs),
                           ("audit", cmd_audit), ("smoke", cmd_smoke),
                           ("worker", cmd_worker), ("capability", cmd_capability),
                           ("finalize", cmd_finalize)):
        item = sub.add_parser(name)
        item.add_argument("--package", type=Path, default=PACKAGE)
        item.set_defaults(func=function)
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except SystemExit:
        raise
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__,
                          "detail": str(exc)[:500]}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
