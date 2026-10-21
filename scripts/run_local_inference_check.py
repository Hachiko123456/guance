"""Repeatable local source inference check for auto/cpu/cuda device selection.

This is a thin, explicit entry point. The caller names a trusted interpreter; no
bundle, manifest or descriptor can choose an executable. Each mode runs the real
production JSONL client/worker through ``run_gpu_first_inference_acceptance.py``
and records the interpreter, the actual device/reason, and the recommendation
outcome. Only this child process gets a temporary ``GUANCE_DATA_DIR``; the global
``PATH``, the persistent preference and the accepted CPU environment are untouched.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_gpu_first_inference_acceptance.py"
GPU_PYTHON = ROOT / "build" / "gpu-env-20261020" / "Scripts" / "python.exe"
GPU_BUNDLES = ROOT / "build" / "acceptance" / "guance-gpu-first-inference-20261020" / "bundles"
CPU_BUNDLES = ROOT / "build" / "acceptance" / "guance-gpu-first-inference-20261020" / "bundles-cpu-env"
ITER2000_BUNDLE = "marvel-v2-iter002000-device"

MODES = {
    "gpu-auto": {"device": "auto", "bundles": GPU_BUNDLES, "command": "smoke"},
    "forced-cpu": {"device": "cpu", "bundles": GPU_BUNDLES, "command": "smoke"},
    "no-cuda-hidden": {"device": None, "bundles": GPU_BUNDLES, "command": "no-gpu"},
    "cpu-only-fail-closed": {"device": None, "bundles": CPU_BUNDLES, "command": "no-gpu"},
    "cpu-only-auto": {"device": "auto", "bundles": CPU_BUNDLES, "command": "smoke"},
}


class LaunchError(RuntimeError):
    pass


def validate_interpreter(path: Path) -> Path:
    if not path.is_absolute():
        raise LaunchError("interpreter path must be absolute")
    resolved = path.resolve()
    if not resolved.is_file():
        raise LaunchError(f"interpreter does not exist: {resolved}")
    if resolved.suffix.lower() not in {".exe"}:
        raise LaunchError("interpreter must be a Windows .exe")
    forbidden_roots = (
        ROOT / "data",
        ROOT / "runtime",
        ROOT / "build" / "acceptance",
    )
    if any(resolved.is_relative_to(root.resolve()) for root in forbidden_roots):
        raise LaunchError("interpreter may not come from data/, runtime/ or the acceptance bundles")
    return resolved


def probe_interpreter(python: Path) -> dict[str, Any]:
    script = (
        "import json,sys,struct\n"
        "out={'version':sys.version,'pointer_bits':struct.calcsize('P')*8,"
        "'executable':sys.executable}\n"
        "try:\n"
        "    import torch\n"
        "    out['torch_version']=torch.__version__\n"
        "    out['torch_cuda_build']=torch.version.cuda\n"
        "    out['cuda_available']=bool(torch.cuda.is_available())\n"
        "    out['device_name']=torch.cuda.get_device_name(0) if torch.cuda.is_available() else ''\n"
        "except Exception as exc:\n"
        "    out['torch_error']=type(exc).__name__\n"
        "print(json.dumps(out,sort_keys=True))\n"
    )
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run([str(python), "-B", "-I", "-c", script], env=env,
                            capture_output=True, text=True, timeout=120, check=False)
    if result.returncode:
        raise LaunchError(f"interpreter probe failed: {result.stderr[-500:]}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def build_command(mode: str, python: Path, output: Path) -> list[str]:
    if mode not in MODES:
        raise LaunchError(f"unknown mode: {mode}")
    config = MODES[mode]
    command = [str(python), "-B", str(RUNNER), config["command"],
               "--bundle-dir", str(config["bundles"] / ITER2000_BUNDLE),
               "--output", str(output)]
    if config["device"] is not None:
        command += ["--device", config["device"]]
    return command


def child_env(mode: str, isolated_data: Path) -> dict[str, str]:
    """Return the child environment without mutating the parent PATH or settings."""
    if mode not in MODES:
        raise LaunchError(f"unknown mode: {mode}")
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["GUANCE_DATA_DIR"] = str(isolated_data.resolve())
    env["GUANCE_MODEL_DEVICE"] = MODES[mode]["device"] or "auto"
    if mode == "no-cuda-hidden":
        env["CUDA_VISIBLE_DEVICES"] = ""
    return env


def run_mode(mode: str, python: Path, output: Path, *, artifact: Path | None = None,
             isolated_data: Path, timeout: float = 900.0) -> dict[str, Any]:
    python = validate_interpreter(python)
    if mode not in MODES:
        raise LaunchError(f"unknown mode: {mode}")
    output.parent.mkdir(parents=True, exist_ok=True)
    isolated_data.mkdir(parents=True, exist_ok=True)
    artifact = (artifact or output.with_name(output.stem + "-artifact.json")).resolve()
    command = build_command(mode, python, artifact)
    env = child_env(mode, isolated_data)
    started = datetime.now(timezone.utc).astimezone()
    try:
        result = subprocess.run(
            command, cwd=ROOT, env=env, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
        exit_code, stdout, stderr = result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired as exc:
        exit_code = -1
        stdout = exc.stdout or ""
        stderr = f"TIMEOUT after {timeout}s"
    elapsed = (datetime.now(timezone.utc).astimezone() - started).total_seconds()
    log_path = output.with_suffix(".log")
    log_path.write_text(
        f"$ {' '.join(command)}\nexit={exit_code} elapsed={elapsed:.1f}s\n"
        f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}\n", encoding="utf-8")
    produced = None
    if artifact.is_file():
        produced = json.loads(artifact.read_text(encoding="utf-8"))
    if MODES[mode]["command"] == "no-gpu":
        expected = exit_code == 0 and produced is not None
        check = ("no-CUDA worker: auto->cpu with a classified reason, forced cuda fails "
                 "MODEL_UNAVAILABLE, forced cpu stays ready")
    else:
        expected = exit_code == 0 and produced is not None
        check = "all models ready with one legal recommendation"
    report = {
        "schema": "guance-local-inference-check-v1",
        "mode": mode,
        "command": command,
        "interpreter": probe_interpreter(python),
        "isolated_data_dir": str(isolated_data.resolve()),
        "exit": exit_code,
        "elapsed_seconds": round(elapsed, 3),
        "stdout": stdout.strip()[-4000:],
        "stderr": stderr.strip()[-4000:],
        "artifact": str(artifact),
        "artifact_present": produced is not None,
        "artifact_content": produced,
        "expected": check,
        "status": "passed" if expected else "failed",
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, default=GPU_PYTHON,
                        help="trusted interpreter (absolute path)")
    parser.add_argument("--mode", choices=tuple(MODES), required=True)
    parser.add_argument("--output", type=Path, required=True,
                        help="launcher report JSON path")
    parser.add_argument("--artifact", type=Path, default=None,
                        help="runner artifact path (default: <output>-artifact.json)")
    parser.add_argument("--isolated-data", type=Path, default=None)
    parser.add_argument("--timeout", type=float, default=900.0)
    args = parser.parse_args(list(argv) if argv is not None else None)
    output = args.output.resolve()
    isolated = (args.isolated_data or output.with_name(output.stem + "-data")).resolve()
    try:
        report = run_mode(args.mode, args.python, output, artifact=args.artifact,
                          isolated_data=isolated, timeout=args.timeout)
    except LaunchError as exc:
        report = {"schema": "guance-local-inference-check-v1", "mode": args.mode,
                  "status": "failed", "error": str(exc)}
        print(json.dumps(report, sort_keys=True))
        return 2
    summary = {
        key: report[key] for key in
        ("schema", "mode", "status", "exit", "elapsed_seconds", "artifact")
    }
    summary["interpreter"] = report["interpreter"]
    summary["report"] = str(output)
    print(json.dumps(summary, sort_keys=True))
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
