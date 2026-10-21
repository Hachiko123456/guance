"""Final local GPU offline delivery acceptance for the 2026-10-20 task.

This runner only operates on an extracted final ZIP (never the build tree) for
the real GUI/worker checks.  It never starts capture, changes proxy/certificate
state, executes game actions or contacts the network.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from typing import Any

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT))
sys.path.insert(0, str(CODE_ROOT / "src"))
sys.dont_write_bytecode = True

EVIDENCE = CODE_ROOT / "build" / "acceptance" / "guance-final-local-gpu-delivery-20261020"
BUNDLE_RELATIVE = Path("_internal") / "marvel_runtime" / "bundles" / "marvel-v2-iter002000-device"
PACKAGE_BUNDLE_RELATIVE = Path("_internal") / "marvel_runtime" / "bundles" / "marvel-v2-iter002000-device"
FORBIDDEN_PARTS = {".git", "private_data", "logs", "debug", "captures", "prototypes"}
# Vendored Torch ships its own ``debug``/``logs`` subpackages; those are runtime
# code, not Guance captures or private logs.  Keep the hard-forbidden parts
# everywhere and apply the Guance-local debug/log rule only outside the
# explicit local runtime allowlist.
STRICT_FORBIDDEN_PARTS = {".git", "private_data", "captures", "prototypes"}
GUANCE_LOCAL_FORBIDDEN_PARTS = {"debug", "logs"}
FORBIDDEN_NAMES = {
    "python314.dll", "proxy-lease.json", "preferences.json", "active_model.json",
    "cookie", "cookies", "authorization.json",
}
FORBIDDEN_SUFFIXES = {".pem", ".p12", ".pfx", ".key", ".cer", ".crt", ".der", ".log",
                      ".pcap", ".pcapng", ".dmp", ".mitm"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def _inventory(root: Path) -> dict[str, tuple[int, str]]:
    return {
        path.relative_to(root).as_posix(): (path.stat().st_size, _sha256(path))
        for path in root.rglob("*") if path.is_file()
    }


def _clean_env(data_dir: Path, *, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE",
                 "GUANCE_MODEL_BUNDLE", "GUANCE_MODEL_PATH",
                 "GUANCE_MARVEL_V2_SOURCE", "GUANCE_MARVEL_V2_CHECKPOINT",
                 "GUANCE_MARVEL_SOURCE", "GUANCE_MARVEL_CHECKPOINT",
                 "CUDA_VISIBLE_DEVICES", "GUANCE_PACKAGE_PROFILE"):
        env.pop(name, None)
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONNOUSERSITE="1",
               PYTHONSAFEPATH="1", PYTHONDONTWRITEBYTECODE="1",
               GUANCE_DATA_DIR=str(data_dir))
    if extra:
        env.update(extra)
    return env


def _run(command: list[str], *, env: dict[str, str], timeout: float) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", env=env, timeout=timeout,
                                cwd=str(Path(command[0]).resolve().parent))
        return {"command": command, "exit": result.returncode,
                "stdout": result.stdout[-8000:], "stderr": result.stderr[-8000:],
                "elapsed": round(time.perf_counter() - started, 3), "timeout": False}
    except subprocess.TimeoutExpired as exc:
        return {"command": command, "exit": None, "stdout": (exc.stdout or "")[-8000:],
                "stderr": (exc.stderr or "")[-8000:],
                "elapsed": round(time.perf_counter() - started, 3), "timeout": True}


def _package_paths(package: Path) -> dict[str, Path]:
    return {
        "exe": package / "Guance.exe",
        "internal": package / "_internal",
        "bundle": package / PACKAGE_BUNDLE_RELATIVE,
        "best_npz": package / "_internal" / "data" / "profiles" / "tencent_daguandan"
                    / "models" / "best.npz",
        "checkpoint": package / "_internal" / "marvel_runtime" / "checkpoint"
                      / "m09_candidate_iter002000.pt",
        "torch": package / "_internal" / "marvel_runtime" / "site-packages" / "torch",
        "source": package / "_internal" / "marvel_runtime" / "source",
    }


# ------------------------------------------------------------------ audit
def _pe_machine(path: Path) -> int | None:
    try:
        import pefile
    except ImportError:  # pragma: no cover - pefile is a build/test dependency
        return None
    try:
        return int(pefile.PE(str(path), fast_load=True).FILE_HEADER.Machine)
    except Exception:
        return None


def cmd_audit(args: argparse.Namespace) -> int:
    package = args.package.resolve()
    out = args.out.resolve()
    paths = _package_paths(package)
    files = _inventory(package)
    required = [
        package / "Guance.exe", package / "_internal", package / "START_HERE.md",
        package / "NOTICE.md", package / "README.md", package / "capability-manifest.json",
        package / "SHA256SUMS", paths["best_npz"], paths["checkpoint"], paths["bundle"],
        paths["torch"] / "__init__.py", paths["source"] / "guandan_marvel" / "__init__.py",
    ]
    missing = [str(path.relative_to(package)) for path in required if not path.exists()]
    forbidden = []
    for relative in files:
        pure = PurePosixPath(relative)
        lowered_parts = {part.lower() for part in pure.parts}
        if lowered_parts & STRICT_FORBIDDEN_PARTS:
            forbidden.append(relative)
        elif (lowered_parts & GUANCE_LOCAL_FORBIDDEN_PARTS
              and not relative.startswith("_internal/marvel_runtime/")):
            forbidden.append(relative)
        if pure.name.lower() in FORBIDDEN_NAMES or pure.suffix.lower() in FORBIDDEN_SUFFIXES:
            forbidden.append(relative)
        if "captures" in relative.lower() and not relative.startswith("_internal/marvel_runtime"):
            forbidden.append(relative)
    unexpected_models = []
    allowed_model_assets = {
        "_internal/data/profiles/tencent_daguandan/models/best.npz",
        "_internal/marvel_runtime/checkpoint/m09_candidate_iter002000.pt",
    }
    for relative in files:
        suffix = PurePosixPath(relative).suffix.lower()
        if suffix in {".pt", ".pth", ".ckpt", ".npz", ".onnx", ".safetensors"}:
            if relative not in allowed_model_assets:
                unexpected_models.append(relative)
    torch_version = (paths["torch"] / "version.py").read_text(encoding="utf-8") if (
        paths["torch"] / "version.py").is_file() else ""
    version_run = _run([str(paths["exe"]), "--version"], env=_clean_env(out / "data"),
                       timeout=120)
    machines = {
        "Guance.exe": _pe_machine(paths["exe"]),
        "torch_cuda.dll": _pe_machine(paths["torch"] / "lib" / "torch_cuda.dll"),
    }
    checks = {
        "required_present": not missing,
        "forbidden_absent": not forbidden,
        "single_allowlisted_models": not unexpected_models,
        "torch_cu126_declared": '"2.13.0+cu126"' in torch_version or "2.13.0+cu126" in torch_version,
        "capture_runtime_absent": not (package / "_internal" / "runtime" / "mitmproxy").exists(),
        "python314_absent": not any(name.lower() == "python314.dll" for name in
                                    (PurePosixPath(item).name for item in files)),
        "exe_version_exit_zero": version_run["exit"] == 0,
        "pe_x64": all(value == 0x8664 for value in machines.values() if value is not None),
    }
    report = {
        "schema": "guance-final-gpu-package-audit-v1",
        "package": str(package),
        "file_count": len(files),
        "tree_bytes": sum(size for size, _ in files.values()),
        "missing": missing,
        "forbidden": sorted(set(forbidden)),
        "unexpected_model_assets": unexpected_models,
        "checks": checks,
        "version_run": {key: version_run[key] for key in ("exit", "stdout", "stderr")},
        "pe_machine": machines,
        "status": "passed" if all(checks.values()) else "failed",
    }
    _write(out / "audit.json", report)
    print(json.dumps({"status": report["status"], "files": len(files),
                      "bytes": report["tree_bytes"], "checks": checks}, sort_keys=True))
    return 0 if report["status"] == "passed" else 2


# --------------------------------------------------------------- zip/extract
def cmd_extract(args: argparse.Namespace) -> int:
    archive = args.zip.resolve()
    out = args.out.resolve()
    digest = _sha256(archive)
    target = Path(tempfile.mkdtemp(prefix="Guance final verification 20261020 "))
    target = target / "含空格 中文 解压目录"
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        bad = bundle.testzip()
        names = bundle.namelist()
        for name in names:
            pure = PurePosixPath(name)
            if pure.is_absolute() or ".." in pure.parts or "\\" in name:
                raise SystemExit(f"unsafe zip member: {name}")
        bundle.extractall(target)
    roots = sorted({PurePosixPath(name).parts[0] for name in names if name.strip("/")})
    package = target / roots[0] if len(roots) == 1 else target
    report = {
        "schema": "guance-final-gpu-zip-extract-v1",
        "zip": str(archive), "zip_sha256": digest, "zip_bytes": archive.stat().st_size,
        "member_count": len(names), "testzip": bad,
        "extract_root": str(target), "package_root": str(package),
        "package_exists": (package / "Guance.exe").is_file(),
        "status": "passed" if bad is None and (package / "Guance.exe").is_file() else "failed",
    }
    _write(out / "zip-extract.json", report)
    (out / "extracted-package-path.txt").write_text(str(package), encoding="utf-8")
    print(json.dumps({"status": report["status"], "package": str(package),
                      "zip_sha256": digest}, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "passed" else 2


def cmd_verify_manifest(args: argparse.Namespace) -> int:
    package = args.package.resolve()
    sums = package / "SHA256SUMS"
    failures = []
    seen = set()
    for line in sums.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, _, relative = line.partition("  ")
        seen.add(relative)
        target = package.joinpath(*PurePosixPath(relative).parts)
        if not target.is_file() or _sha256(target) != digest:
            failures.append(relative)
    actual = _inventory(package)
    missing_entries = sorted(set(actual) - seen - {"SHA256SUMS"})
    report = {
        "schema": "guance-final-gpu-manifest-verify-v1",
        "manifest_entries": len(seen), "package_files": len(actual),
        "failures": failures, "missing_entries": missing_entries,
        "self_excluded": "SHA256SUMS" not in seen,
        "status": "passed" if not failures and not missing_entries else "failed",
    }
    _write(args.out.resolve() / "manifest-verify.json", report)
    print(json.dumps({"status": report["status"], "entries": len(seen)}, sort_keys=True))
    return 0 if report["status"] == "passed" else 2


# ------------------------------------------------------------------- smoke
def cmd_smoke(args: argparse.Namespace) -> int:
    package = args.package.resolve()
    out = args.out.resolve()
    data_dir = out / "smoke-data"
    report_path = out / "smoke-report.json"
    result = _run([str(package / "Guance.exe"), "--frozen-smoke",
                   "--smoke-report", str(report_path), "--exit-after", "12"],
                  env=_clean_env(data_dir), timeout=180)
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
    good = (result["exit"] == 0 and report.get("status") == "passed"
            and report.get("qml_engine_created") is True
            and report.get("main_window_created") is True
            and report.get("root_visible") is True
            and not any(report.get("module_guards", {}).values())
            and not report.get("qt_messages"))
    summary = {
        "schema": "guance-final-gpu-frozen-smoke-v1",
        "run": result, "report": report, "status": "passed" if good else "failed",
    }
    _write(out / "smoke.json", summary)
    print(json.dumps({"status": summary["status"], "exit": result["exit"]}, sort_keys=True))
    return 0 if good else 2


# --------------------------------------------------------------------- gui
def cmd_gui(args: argparse.Namespace) -> int:
    package = args.package.resolve()
    out = args.out.resolve()
    data_dir = out / "gui-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    lease = data_dir / "config" / "proxy-lease.json"
    if lease.exists():
        raise SystemExit("isolated GUI data dir unexpectedly contains a proxy lease")
    env = _clean_env(data_dir)
    first = _run([str(package / "Guance.exe"), "--exit-after", "8"], env=env, timeout=180)
    second = _run([str(package / "Guance.exe"), "--exit-after", "8"], env=env, timeout=180)
    leftovers = sorted(path.relative_to(data_dir).as_posix()
                       for path in data_dir.rglob("*") if path.is_file())
    forbidden = [name for name in leftovers if name.lower().endswith(
        (".pem", ".cer", ".crt", ".key", ".p12", ".pfx")) or "proxy-lease" in name.lower()]
    qt_errors = [line for line in (first["stderr"] + "\n" + second["stderr"]).splitlines()
                 if any(token in line for token in
                        ("QQmlApplicationEngine failed", "Cannot assign", "module \"",
                         "is not installed", "QML debugging"))]
    good = (first["exit"] == 0 and second["exit"] == 0 and not forbidden and not qt_errors
            and not lease.exists())
    summary = {
        "schema": "guance-final-gpu-gui-v1",
        "isolated_data_dir": str(data_dir),
        "proxy_lease_before": False, "proxy_lease_after": lease.exists(),
        "first_launch": first, "second_launch": second,
        "data_files": leftovers, "certificate_or_lease_files": forbidden,
        "qt_error_lines": qt_errors,
        "side_effects": {"capture_started": False, "proxy_changed": False,
                         "certificate_installed": False, "network_access": False},
        "status": "passed" if good else "failed",
    }
    _write(out / "gui.json", summary)
    print(json.dumps({"status": summary["status"], "first": first["exit"],
                      "second": second["exit"]}, sort_keys=True))
    return 0 if good else 2


# ---------------------------------------------------------------- workers
def _worker_modules():
    frozen = importlib.import_module("scripts.run_offline_delivery_acceptance")
    gpu = importlib.import_module("scripts.run_gpu_first_inference_acceptance")
    return frozen, gpu


def cmd_fabledan(args: argparse.Namespace) -> int:
    package = args.package.resolve()
    out = args.out.resolve()
    frozen, _gpu = _worker_modules()
    data_dir = out / "fabledan-data"
    env = _clean_env(data_dir)
    close = frozen._run_fabledan_session(package, env, stop=False)
    stop = frozen._run_fabledan_session(package, env, stop=True)
    good = close.get("status") == "passed" and stop.get("status") == "passed"
    summary = {
        "schema": "guance-final-gpu-fabledan-v1",
        "close": close, "stop": stop, "status": "passed" if good else "failed",
    }
    _write(out / "fabledan.json", summary)
    print(json.dumps({"status": summary["status"]}, sort_keys=True))
    return 0 if good else 2


def _first_confirmed_payload(gpu_module) -> tuple[dict[str, Any], Any]:
    _replay, classification = gpu_module._prepare_fixed()
    item = classification["eligible"][0]
    return item["payload"], item["context"]


def _positive_marvel_session(package: Path, out: Path, device: str,
                             expect_device: str | None) -> dict[str, Any]:
    _frozen, gpu = _worker_modules()
    data_dir = out / f"marvel-{device}-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    saved = {}
    for name in ("GUANCE_MARVEL_V2_SOURCE", "GUANCE_MARVEL_V2_CHECKPOINT"):
        saved[name] = os.environ.pop(name, None)
    os.environ["GUANCE_DATA_DIR"] = str(data_dir)
    worker = None
    try:
        worker = gpu._Worker(package / PACKAGE_BUNDLE_RELATIVE, device,
                             workload_timeout=600.0, executable=package / "Guance.exe")
        status = worker.start()
        metadata = gpu._device_summary(worker.metadata)
        if expect_device is not None and metadata.get("actual_device") != expect_device:
            gpu._fail("UNEXPECTED_DEVICE")
        payload, context = _first_confirmed_payload(gpu)
        oracle = gpu.VerifiedGuandanRulesOracle(gpu.RUNTIME_SOURCE)
        before = gpu._hash_json(payload)
        result = worker.submit(payload)
        if gpu._hash_json(payload) != before or before != context.payload_hash:
            gpu._fail("MODEL_INPUT_MUTATED")
        validated = gpu.validate_recommendation(context, result, oracle)
        record = {
            "status": "passed", "loaded_status": status.get("model_status"),
            "metadata": metadata, "recommendation": validated,
            "recommendation_count": 1, "model_fallback_count": 0,
            "input_immutable": True,
        }
    except Exception as exc:  # noqa: BLE001 - bounded acceptance evidence
        record = {"status": "failed", "reason": gpu._bounded_code(getattr(exc, "code", exc))}
    finally:
        if worker is not None:
            try:
                worker.close()
            except Exception:
                pass
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value
            else:
                os.environ.pop(name, None)
        os.environ.pop("GUANCE_DATA_DIR", None)
    return record


def cmd_marvel(args: argparse.Namespace) -> int:
    package = args.package.resolve()
    out = args.out.resolve()
    gpu_auto = _positive_marvel_session(package, out, "auto", "cuda:0")
    forced_cpu = _positive_marvel_session(package, out, "cpu", "cpu")
    _frozen, gpu = _worker_modules()
    data_dir = out / "forced-cuda-data"
    env = _clean_env(data_dir)
    saved = {name: os.environ.pop(name, None) for name in
             ("GUANCE_MARVEL_V2_SOURCE", "GUANCE_MARVEL_V2_CHECKPOINT")}
    os.environ["GUANCE_DATA_DIR"] = str(data_dir)
    # Forced-cuda fail-closed is proven on a masked device only.  Without the
    # mask a CUDA-capable host must succeed at forced cuda, so the negative
    # would be meaningless.
    saved_mask = os.environ.get("CUDA_VISIBLE_DEVICES")
    os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
    try:
        worker = gpu._Worker(package / PACKAGE_BUNDLE_RELATIVE, "cuda",
                             workload_timeout=180.0, executable=package / "Guance.exe")
        try:
            worker.start()
            forced_cuda = {"status": "failed", "reason": "GPU_NOT_REQUIRED_TO_FAIL"}
        except Exception as exc:
            forced_cuda = {"status": "passed", "error_code": gpu._bounded_code(getattr(exc, "code", exc)),
                           "recommendation_count": 0, "model_fallback_count": 0}
        finally:
            try:
                worker.close()
            except Exception:
                pass
    finally:
        if saved_mask is None:
            os.environ.pop("CUDA_VISIBLE_DEVICES", None)
        else:
            os.environ["CUDA_VISIBLE_DEVICES"] = saved_mask
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value
            else:
                os.environ.pop(name, None)
        os.environ.pop("GUANCE_DATA_DIR", None)
    good = (gpu_auto.get("status") == "passed" and forced_cpu.get("status") == "passed"
            and forced_cuda.get("status") == "passed")
    summary = {
        "schema": "guance-final-gpu-marvel-device-v1",
        "auto_gpu": gpu_auto, "forced_cpu": forced_cpu, "forced_cuda": forced_cuda,
        "env_for_worker": {key: env.get(key) for key in
                           ("PYTHONPATH", "PYTHONHOME", "GUANCE_MARVEL_V2_SOURCE",
                            "GUANCE_MARVEL_V2_CHECKPOINT", "CUDA_VISIBLE_DEVICES")},
        "status": "passed" if good else "failed",
    }
    _write(out / "marvel-device.json", summary)
    print(json.dumps({"status": summary["status"]}, sort_keys=True))
    return 0 if good else 2


def cmd_fixed(args: argparse.Namespace) -> int:
    package = args.package.resolve()
    out = args.out.resolve()
    _frozen, gpu = _worker_modules()
    bundle = package / PACKAGE_BUNDLE_RELATIVE
    exe = package / "Guance.exe"
    saved = {name: os.environ.pop(name, None) for name in
             ("GUANCE_MARVEL_V2_SOURCE", "GUANCE_MARVEL_V2_CHECKPOINT",
              "CUDA_VISIBLE_DEVICES")}
    results = {}
    try:
        results["gpu_auto"] = _fixed_run(gpu, bundle, "auto", out / "fixed-input-gpu.json",
                                         exe, "cuda:0")
        results["forced_cpu"] = _fixed_run(gpu, bundle, "cpu", out / "fixed-input-cpu.json",
                                           exe, "cpu")
        os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
        results["masked_cpu"] = _fixed_run(gpu, bundle, "auto", out / "fixed-input-masked.json",
                                           exe, "cpu")
        os.environ.pop("CUDA_VISIBLE_DEVICES", None)
    finally:
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value
            else:
                os.environ.pop(name, None)
    good = all(item.get("exit") == 0 for item in results.values())
    summary = {"schema": "guance-final-gpu-fixed-matrix-v1", "results": results,
               "status": "passed" if good else "failed"}
    _write(out / "fixed-matrix.json", summary)
    print(json.dumps({"status": summary["status"],
                      "results": {key: value.get("summary", value.get("stdout", ""))[:200]
                                  for key, value in results.items()}}, sort_keys=True))
    return 0 if good else 2


def _fixed_run(gpu, bundle: Path, device: str, output: Path, exe: Path,
               expect: str) -> dict[str, Any]:
    try:
        gpu.run_fixed(bundle, device, output, workload_timeout=900.0, executable=exe,
                      packaged_env=True, expect_device=expect)
        data = json.loads(output.read_text(encoding="utf-8"))
        summary = {key: data.get(key) for key in (
            "recommendation_count", "provider_eligible_count", "strict_eligible_count",
            "strict_ineligible_count", "model_fallback_count", "input_immutable",
            "device_fallback")}
        summary["actual_device"] = (data.get("runtime") or {}).get("actual_device")
        summary["device_reason"] = (data.get("runtime") or {}).get("device_reason")
        return {"exit": 0, "summary": json.dumps(summary, ensure_ascii=False, sort_keys=True),
                "record": summary}
    except Exception as exc:  # noqa: BLE001
        return {"exit": 2, "reason": gpu._bounded_code(getattr(exc, "code", exc))}


def cmd_negatives(args: argparse.Namespace) -> int:
    package = args.package.resolve()
    out = args.out.resolve()
    frozen, gpu = _worker_modules()
    data_dir = out / "negative-data"
    env = _clean_env(data_dir)
    results: dict[str, Any] = {}
    # Missing model.
    results["missing_model"] = frozen._run_missing_model(package, env)
    # Tampered descriptor bundle (copy only; never modify the package).
    tampered = Path(tempfile.mkdtemp(prefix="guance-tampered-bundle-"))
    shutil.copytree(package / PACKAGE_BUNDLE_RELATIVE, tampered / "bundle")
    descriptor = tampered / "bundle" / "deployment.json"
    descriptor.write_bytes(descriptor.read_bytes() + b" ")
    results["tampered_descriptor"] = _load_expect_error(
        frozen, package, env, tampered / "bundle", {"ARTIFACT_HASH_MISMATCH", "MANIFEST_INVALID",
                                                     "MODEL_VERSION_UNSUPPORTED", "DEPLOYMENT_INVALID"})
    # Tampered checkpoint via explicit env override.
    tampered_ckpt = Path(tempfile.mkdtemp(prefix="guance-tampered-ckpt-")) / "m09_candidate_iter002000.pt"
    original = package / "_internal" / "marvel_runtime" / "checkpoint" / "m09_candidate_iter002000.pt"
    tampered_ckpt.write_bytes(original.read_bytes()[:1024] + b"x" + original.read_bytes()[1025:])
    ckpt_env = dict(env)
    ckpt_env["GUANCE_MARVEL_V2_CHECKPOINT"] = str(tampered_ckpt)
    results["tampered_checkpoint"] = _load_expect_error(
        frozen, package, ckpt_env, package / PACKAGE_BUNDLE_RELATIVE,
        {"ARTIFACT_HASH_MISMATCH", "MODEL_VERSION_UNSUPPORTED", "MODEL_UNAVAILABLE"})
    # Confirmed request against FableDan must fail closed without switching models.
    payload, _context = _first_confirmed_payload(gpu)
    results["stale_after_switch"] = _switch_stale_check(frozen, gpu, package, env, payload)
    # Malformed wire contract must be rejected before inference.
    malformed = frozen._envelope("recommend", "malformed-decision",
                                 request_kind="confirmed_decision", decision=payload)
    malformed["unexpected"] = True
    results["malformed_contract"] = _malformed_check(frozen, package, env, malformed)
    good = all(item.get("status") == "passed" for item in results.values())
    summary = {"schema": "guance-final-gpu-negatives-v1", "results": results,
               "status": "passed" if good else "failed"}
    _write(out / "negatives.json", summary)
    print(json.dumps({"status": summary["status"],
                      "items": {key: item.get("status") for key, item in results.items()}},
                     sort_keys=True))
    return 0 if good else 2


def _load_expect_error(frozen, package: Path, env: dict[str, str], source: Path,
                       allowed: set[str]) -> dict[str, Any]:
    session = frozen.WorkerSession(package / "Guance.exe", env)
    try:
        session.send(frozen._envelope("load", "negative-load", source=str(source)))
        deadline = time.monotonic() + 180
        reply = None
        while time.monotonic() < deadline:
            reply = session.reply(max(0.1, deadline - time.monotonic()))
            if reply is None or reply.get("type") in {"error", "loaded"}:
                break
        final = session.close(graceful=True)
        code = reply.get("error_code") if reply else None
        good = (reply is not None and reply.get("type") == "error"
                and code in allowed and final["exit"] == 0)
        return {"status": "passed" if good else "failed", "reply_type": reply.get("type") if reply else None,
                "error_code": code, "recommendation_count": 0, "final": final}
    finally:
        if session.process.poll() is None:
            session.process.kill()


def _switch_stale_check(frozen, gpu, package: Path, env: dict[str, str],
                        payload: dict[str, Any]) -> dict[str, Any]:
    session = frozen.WorkerSession(package / "Guance.exe", env)
    try:
        session.send(frozen._envelope("load", "switch-1",
                                      source=str(package / PACKAGE_BUNDLE_RELATIVE)))
        _wait_types(session, {"loaded", "error"}, 180)
        session.send(frozen._envelope("load", "switch-2",
                                      source=str(package / "_internal" / "data" / "profiles"
                                                 / "tencent_daguandan" / "models" / "best.npz")))
        loaded = _wait_types(session, {"loaded", "error"}, 180)
        session.send(frozen._envelope("recommend", "stale-decision",
                                      request_kind="confirmed_decision", decision=payload))
        reply = _wait_types(session, {"result", "error"}, 120)
        final = session.close(graceful=True)
        good = (loaded is not None and loaded.get("type") == "loaded"
                and reply is not None and reply.get("type") == "error"
                and reply.get("error_code") == "REQUEST_KIND_UNSUPPORTED"
                and final["exit"] == 0)
        return {"status": "passed" if good else "failed", "loaded_type": loaded.get("type") if loaded else None,
                "reply_type": reply.get("type") if reply else None,
                "error_code": reply.get("error_code") if reply else None,
                "recommendation_count": 0, "final": final}
    finally:
        if session.process.poll() is None:
            session.process.kill()


def _malformed_check(frozen, package: Path, env: dict[str, str],
                     message: dict[str, Any]) -> dict[str, Any]:
    session = frozen.WorkerSession(package / "Guance.exe", env)
    try:
        session.send(frozen._envelope("load", "malformed-load",
                                      source=str(package / PACKAGE_BUNDLE_RELATIVE)))
        loaded = _wait_types(session, {"loaded", "error"}, 300)
        session.send(message)
        reply = _wait_types(session, {"result", "error"}, 120)
        final = session.close(graceful=False)
        good = (loaded is not None and loaded.get("type") == "loaded"
                and reply is not None and reply.get("type") == "error"
                and reply.get("error_code") == "PROTOCOL_INVALID"
                and final["exit"] == 3)
        return {"status": "passed" if good else "failed",
                "reply_type": reply.get("type") if reply else None,
                "error_code": reply.get("error_code") if reply else None,
                "final": final}
    finally:
        if session.process.poll() is None:
            session.process.kill()


def _wait_types(session, kinds: set[str], timeout: float) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        reply = session.reply(max(0.1, deadline - time.monotonic()))
        if reply is None:
            return None
        if reply.get("type") in kinds:
            return reply
    return None


def cmd_cleanup(args: argparse.Namespace) -> int:
    package = args.package.resolve()
    out = args.out.resolve()
    frozen, _gpu = _worker_modules()
    env = _clean_env(out / "cleanup-data")
    session = frozen.WorkerSession(package / "Guance.exe", env)
    pid = session.process.pid
    session.send(frozen._envelope("close", "cleanup-close"))
    reply = _wait_types(session, {"status"}, 10)
    final = session.close(graceful=True)
    # Abnormal path: force kill and prove the process is reaped.
    session2 = frozen.WorkerSession(package / "Guance.exe", env)
    pid2 = session2.process.pid
    session2.process.kill()
    exit2 = session2.process.wait(timeout=10)
    alive = _alive(pid) or _alive(pid2)
    summary = {
        "schema": "guance-final-gpu-cleanup-v1",
        "graceful_pid": pid, "graceful_reply": reply, "graceful_final": final,
        "abnormal_pid": pid2, "abnormal_exit": exit2,
        "owned_processes_alive_after": alive,
        "status": "passed" if (final["exit"] == 0 and not alive) else "failed",
    }
    _write(out / "cleanup.json", summary)
    print(json.dumps({"status": summary["status"], "alive": alive}, sort_keys=True))
    return 0 if summary["status"] == "passed" else 2


def _alive(pid: int) -> bool:
    try:
        result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                capture_output=True, text=True, timeout=30)
        return str(pid) in result.stdout
    except Exception:
        return False


def cmd_finalize(args: argparse.Namespace) -> int:
    package = args.package.resolve()
    out = args.out.resolve()
    archive = args.zip.resolve()
    required = [package / "Guance.exe", package / "_internal", package / "START_HERE.md",
                package / "NOTICE.md", package / "README.md",
                package / "capability-manifest.json"]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise SystemExit(f"finalize requires complete package payload: {missing}")
    if archive.exists():
        raise SystemExit(f"refusing to overwrite an existing final ZIP: {archive}")
    files = _inventory(package)
    if "SHA256SUMS" in files:
        raise SystemExit("package already contains SHA256SUMS; refusing to re-finalize in place")
    lines = [f"{digest}  {relative}" for relative, (_size, digest) in sorted(files.items())]
    (package / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in sorted(package.rglob("*")):
            if path.is_file():
                bundle.write(path, (PurePosixPath(package.name) /
                                    PurePosixPath(path.relative_to(package).as_posix())).as_posix())
    zip_sha = _sha256(archive)
    report = {
        "schema": "guance-final-gpu-finalize-v1",
        "package": str(package), "package_name": package.name,
        "file_count": len(files) + 1, "tree_bytes": sum(size for size, _ in files.values()),
        "manifest_entries": len(lines), "manifest_excludes_self": True,
        "zip": str(archive), "zip_bytes": archive.stat().st_size, "zip_sha256": zip_sha,
        "zip_test": zipfile.ZipFile(archive).testzip(),
        "exe_sha256": _sha256(package / "Guance.exe"),
        "exe_bytes": (package / "Guance.exe").stat().st_size,
        "git_commit": args.git_commit,
        "status": "passed",
    }
    _write(out / "finalize.json", report)
    print(json.dumps({key: report[key] for key in (
        "zip", "zip_bytes", "zip_sha256", "exe_sha256", "file_count", "git_commit")},
        ensure_ascii=False, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, function, *, package: bool = True, zip_path: bool = False):
        item = sub.add_parser(name)
        if package:
            item.add_argument("--package", type=Path, required=True)
        if zip_path:
            item.add_argument("--zip", type=Path, required=True)
        item.add_argument("--out", type=Path, required=True)
        item.set_defaults(func=function)

    add("audit", cmd_audit)
    add("extract", cmd_extract, package=False, zip_path=True)
    add("verify-manifest", cmd_verify_manifest)
    add("smoke", cmd_smoke)
    add("gui", cmd_gui)
    add("fabledan", cmd_fabledan)
    add("marvel", cmd_marvel)
    add("fixed", cmd_fixed)
    add("negatives", cmd_negatives)
    add("cleanup", cmd_cleanup)
    finalize = sub.add_parser("finalize")
    finalize.add_argument("--package", type=Path, required=True)
    finalize.add_argument("--zip", type=Path, required=True)
    finalize.add_argument("--out", type=Path, required=True)
    finalize.add_argument("--git-commit", required=True)
    finalize.set_defaults(func=cmd_finalize)
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
