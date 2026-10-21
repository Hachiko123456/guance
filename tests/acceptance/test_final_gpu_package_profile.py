"""Regression tests for the final local GPU packaging profile and asset wiring."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "scripts" / "qt_runtime_hook.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _package_assets():
    sys.path.insert(0, str(ROOT / "src"))
    try:
        from guance.infrastructure.model import package_assets
    finally:
        sys.path.pop(0)
    return package_assets


def _build_release():
    return _load("build_release_under_test", ROOT / "scripts" / "build_release.py")


def test_package_assets_absent_outside_frozen(monkeypatch):
    module = _package_assets()
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert module.frozen_runtime_root() is None
    assert module.packaged_marvel_paths() is None
    assert module.apply_packaged_marvel_defaults() is False


def test_package_assets_resolves_declared_package_only(monkeypatch, tmp_path):
    module = _package_assets()
    runtime = tmp_path / "marvel_runtime"
    (runtime / "source").mkdir(parents=True)
    (runtime / "checkpoint").mkdir(parents=True)
    (runtime / "checkpoint" / module.CHECKPOINT_FILENAME).write_bytes(b"x")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.delenv(module.SOURCE_ENV, raising=False)
    monkeypatch.delenv(module.CHECKPOINT_ENV, raising=False)
    # ``apply_packaged_marvel_defaults`` writes os.environ directly (setdefault),
    # so restore the previous state explicitly; otherwise later model tests that
    # read these variables pick up this temporary package root.
    saved = {name: os.environ.pop(name, None)
             for name in (module.SOURCE_ENV, module.CHECKPOINT_ENV)}
    try:
        paths = module.packaged_marvel_paths()
        assert paths is not None
        assert paths["source_root"] == runtime / "source"
        assert paths["checkpoint_path"] == runtime / "checkpoint" / module.CHECKPOINT_FILENAME
        assert module.apply_packaged_marvel_defaults() is True
        assert os.environ[module.SOURCE_ENV] == str(runtime / "source")
        # Explicit values are never overwritten by package defaults.
        monkeypatch.setenv(module.SOURCE_ENV, "D:/explicit/source")
        module.apply_packaged_marvel_defaults()
        assert os.environ[module.SOURCE_ENV] == "D:/explicit/source"
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_package_assets_requires_complete_declared_assets(monkeypatch, tmp_path):
    module = _package_assets()
    (tmp_path / "marvel_runtime" / "source").mkdir(parents=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert module.packaged_marvel_paths() is None


@pytest.mark.skipif(os.name != "nt", reason="Windows DLL search path behavior")
def test_runtime_hook_appends_only_present_vendored_site_packages(tmp_path):
    site = tmp_path / "marvel_runtime" / "site-packages"
    (site / "torch" / "lib").mkdir(parents=True)
    script = (
        "import sys\n"
        f"sys.frozen = True\n"
        f"sys._MEIPASS = {str(tmp_path)!r}\n"
        f"path = {str(HOOK)!r}\n"
        "exec(compile(open(path, encoding='utf-8').read(), path, 'exec'))\n"
        f"assert {str(site)!r} in sys.path\n"
        f"assert {str(site / 'torch' / 'lib')!r} in os.environ['PATH']\n"
        "print('HOOK_OK')\n"
    )
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert "HOOK_OK" in result.stdout


# A frozen windowed worker inherits the ANSI code page for stdio.  This driver is
# executed as a real child process so the regression exercises the hook's effect
# on the actual ``sys.stdin`` text wrapper instead of asserting source text.
_STDIO_DRIVER = r'''
import json
import sys

hook, result_path, mode = sys.argv[1], sys.argv[2], sys.argv[3]
if mode == "gui-none-stdin":
    sys.stdin = None
exec(compile(open(hook, encoding="utf-8").read(), hook, "exec"))
payload = json.loads(sys.argv[4])
record = {"request_id": None, "text": None}
if sys.stdin is not None:
    line = sys.stdin.readline()
    parsed = json.loads(line)
    record = {"request_id": parsed["request_id"], "text": parsed["text"]}
with open(result_path, "w", encoding="utf-8") as stream:
    json.dump({
        "stdin_encoding": getattr(sys.stdin, "encoding", None),
        "stdout_encoding": sys.stdout.encoding,
        "stderr_encoding": sys.stderr.encoding,
        "record": record,
    }, stream, ensure_ascii=False)
print(payload["text"], flush=True)
print(payload["text"], file=sys.stderr, flush=True)
'''


def _run_stdio_driver(tmp_path, *, mode: str, payload: dict[str, str], stdin_bytes: bytes):
    driver = tmp_path / "stdio_driver.py"
    driver.write_text(_STDIO_DRIVER, encoding="utf-8")
    result_path = tmp_path / "stdio_result.json"
    env = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE"):
        env.pop(name, None)
    # Deterministic stand-in for a frozen windowed process: stdio defaults to the
    # ANSI code page unless the runtime hook corrects it.
    env["PYTHONIOENCODING"] = "cp936"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-B", str(driver), str(HOOK), str(result_path), mode,
         json.dumps(payload, ensure_ascii=False)],
        input=stdin_bytes, capture_output=True, timeout=120, env=env,
        cwd=str(tmp_path),
    )
    data = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else {}
    return completed, data


@pytest.mark.skipif(os.name != "nt", reason="Windows ANSI stdio default")
def test_runtime_hook_decodes_utf8_worker_stdin_strictly(tmp_path):
    """A UTF-8 JSONL request with Chinese text must decode intact on stdin.

    The old hook only reconfigured stdout/stderr, so the child decoded the UTF-8
    bytes as cp936: the JSON either raised UnicodeDecodeError or produced
    mojibake, and the exact-text comparison below fails either way.
    """
    payload = {"request_id": "回归-请求", "text": "中文 空格 路径 东风"}
    line = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    completed, data = _run_stdio_driver(tmp_path, mode="worker", payload=payload,
                                        stdin_bytes=line)
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    assert data["record"] == {"request_id": payload["request_id"], "text": payload["text"]}
    for key in ("stdin_encoding", "stdout_encoding", "stderr_encoding"):
        assert data[key].lower().replace("_", "-") == "utf-8", (key, data)
    # stdout/stderr stay UTF-8: strict decoding of the captured bytes must succeed.
    assert completed.stdout.decode("utf-8").strip() == payload["text"]
    assert completed.stderr.decode("utf-8").strip() == payload["text"]


@pytest.mark.skipif(os.name != "nt", reason="Windows ANSI stdio default")
def test_runtime_hook_tolerates_absent_stdin_in_gui_launch(tmp_path):
    """A normal GUI launch exposes no stdin stream; the hook must stay harmless."""
    payload = {"request_id": "gui", "text": "界面 中文"}
    completed, data = _run_stdio_driver(tmp_path, mode="gui-none-stdin", payload=payload,
                                        stdin_bytes=b"")
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    assert data["record"] == {"request_id": None, "text": None}
    assert data["stdout_encoding"].lower().replace("_", "-") == "utf-8"
    assert completed.stdout.decode("utf-8").strip() == payload["text"]


def _marvel_env(monkeypatch, tmp_path, *, checkpoint_hash: str, source_files: int = 120):
    site = tmp_path / "site"
    (site / "torch").mkdir(parents=True)
    (site / "torch" / "__init__.py").write_text("", encoding="utf-8")
    for dependency in ("torchgen", "sympy", "mpmath"):
        (site / dependency).mkdir(parents=True)
        (site / dependency / "__init__.py").write_text("", encoding="utf-8")
    source = tmp_path / "source"
    (source / "nanopokerlib").mkdir(parents=True)
    (source / "guandan_marvel").mkdir(parents=True)
    files = []
    for index in range(source_files):
        relative = f"guandan_marvel/m{index}.py"
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"# {index}\n".encode("utf-8"))
        raw = target.read_bytes()
        import hashlib
        files.append({"path": relative, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    source_hash = {"schema": "guandan-marvel-source-fingerprint-v1",
                   "components": {"materialized-runtime": {"files": files, "hash": "0" * 64}},
                   "hash": "0" * 64}
    native = source / "nanopokerlib" / "native.pyd"
    native.write_bytes(b"native")
    import hashlib
    descriptor = {
        "checkpoint": {"sha256": checkpoint_hash},
        "runtime": {"native_filename": "native.pyd",
                    "native_sha256": hashlib.sha256(b"native").hexdigest(),
                    "native_bytes": len(b"native")},
        "source_fingerprint": source_hash,
    }
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "deployment.json").write_text(json.dumps(descriptor), encoding="utf-8")
    (bundle / "manifest.json").write_text("{}", encoding="utf-8")
    checkpoint = tmp_path / "checkpoint.pt"
    checkpoint.write_bytes(b"checkpoint")
    import hashlib
    sums = hashlib.sha256((bundle / "deployment.json").read_bytes()).hexdigest() + "  deployment.json\n"
    (bundle / "SHA256SUMS").write_text(sums, encoding="utf-8")
    monkeypatch.setenv("GUANCE_MARVEL_SITE_PACKAGES", str(site))
    monkeypatch.setenv("GUANCE_MARVEL_SOURCE", str(source))
    monkeypatch.setenv("GUANCE_MARVEL_CHECKPOINT", str(checkpoint))
    monkeypatch.setenv("GUANCE_MARVEL_BUNDLE", str(bundle))
    return site, source, checkpoint, bundle


def test_marvel_inputs_requires_explicit_environment(monkeypatch):
    module = _build_release()
    for name in ("GUANCE_MARVEL_SITE_PACKAGES", "GUANCE_MARVEL_SOURCE",
                 "GUANCE_MARVEL_CHECKPOINT", "GUANCE_MARVEL_BUNDLE"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError):
        module._marvel_inputs()


def test_marvel_inputs_rejects_wrong_checkpoint_identity(monkeypatch, tmp_path):
    module = _build_release()
    _marvel_env(monkeypatch, tmp_path, checkpoint_hash="f" * 64)
    with pytest.raises(RuntimeError):
        module._marvel_inputs()


def test_install_and_validate_marvel_runtime(tmp_path, monkeypatch):
    module = _build_release()
    site, source, checkpoint, bundle = _marvel_env(
        monkeypatch, tmp_path, checkpoint_hash=module.EXPECTED_MARVEL_CHECKPOINT_SHA256)
    # Use the real expected checkpoint identity for the copy/validate path.
    checkpoint.write_bytes(b"x" * module.EXPECTED_MARVEL_CHECKPOINT_BYTES)
    import hashlib
    digest = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    monkeypatch.setattr(module, "EXPECTED_MARVEL_CHECKPOINT_SHA256", digest)
    descriptor = json.loads((bundle / "deployment.json").read_text(encoding="utf-8"))
    descriptor["checkpoint"]["sha256"] = digest
    (bundle / "deployment.json").write_text(json.dumps(descriptor), encoding="utf-8")
    sums = hashlib.sha256((bundle / "deployment.json").read_bytes()).hexdigest() + "  deployment.json\n"
    (bundle / "SHA256SUMS").write_text(sums, encoding="utf-8")
    inputs = module._marvel_inputs()
    package = tmp_path / "Guance"
    (package / "_internal").mkdir(parents=True)
    (package / "_internal" / "runtime" / "mitmproxy").exists()
    module._install_marvel_runtime(package, inputs)
    installed = package / "_internal" / "marvel_runtime"
    assert (installed / "site-packages" / "torch" / "__init__.py").is_file()
    assert (installed / "site-packages" / "torchgen" / "__init__.py").is_file()
    assert (installed / "site-packages" / "sympy" / "__init__.py").is_file()
    assert (installed / "site-packages" / "mpmath" / "__init__.py").is_file()
    assert (installed / "source" / "guandan_marvel" / "m0.py").is_file()
    assert (installed / "checkpoint" / "checkpoint.pt").is_file()
    assert (installed / "bundles" / "bundle" / "deployment.json").is_file()
    with pytest.raises(RuntimeError):
        module._validate_marvel_package(package, inputs)


def test_install_marvel_runtime_requires_torchgen(tmp_path, monkeypatch):
    module = _build_release()
    _site, _source, _checkpoint, _bundle = _marvel_env(
        monkeypatch, tmp_path, checkpoint_hash=module.EXPECTED_MARVEL_CHECKPOINT_SHA256)
    import shutil
    shutil.rmtree(tmp_path / "site" / "torchgen")
    inputs = {
        "site_packages": tmp_path / "site",
        "source": tmp_path / "source",
        "checkpoint": tmp_path / "checkpoint.pt",
        "bundle": tmp_path / "bundle",
    }
    package = tmp_path / "Guance"
    (package / "_internal").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="torchgen"):
        module._install_marvel_runtime(package, inputs)


def test_validate_marvel_package_requires_torchgen(tmp_path, monkeypatch):
    import shutil
    module, package, _torch_root, inputs = _marvel_package_with_static_lib_branch(
        tmp_path, monkeypatch)
    shutil.rmtree(package / "_internal" / "marvel_runtime" / "site-packages" / "torchgen")
    with pytest.raises(RuntimeError, match="torchgen"):
        module._validate_marvel_package(package, inputs)


def _marvel_package_with_static_lib_branch(tmp_path, monkeypatch):
    """Build a package that satisfies every pointer check before the .lib guard.

    The regression target is the static-library guard at the *end* of
    ``_validate_marvel_package``.  Earlier checks (required files, checkpoint
    identity, descriptor source fingerprint, capture/python314 exclusions) must
    all succeed so the guard is actually reached instead of failing early.
    """
    import hashlib

    module = _build_release()
    package = tmp_path / "Guance"
    runtime = package / "_internal" / "marvel_runtime"
    torch_root = runtime / "site-packages" / "torch"
    (torch_root / "lib").mkdir(parents=True)
    (torch_root / "__init__.py").write_text("", encoding="utf-8")
    (torch_root / "_C.cp312-win_amd64.pyd").write_bytes(b"_C")
    (runtime / "site-packages" / "torchgen").mkdir(parents=True)
    (runtime / "site-packages" / "torchgen" / "__init__.py").write_text("", encoding="utf-8")
    for dependency in ("sympy", "mpmath"):
        (runtime / "site-packages" / dependency).mkdir(parents=True)
        (runtime / "site-packages" / dependency / "__init__.py").write_text(
            "", encoding="utf-8")
    (torch_root / "lib" / "torch_cuda.dll").write_bytes(b"cuda")
    (torch_root / "lib" / "cudart64_12.dll").write_bytes(b"cudart")
    source = runtime / "source"
    (source / "nanopokerlib").mkdir(parents=True)
    (source / "guandan_marvel").mkdir(parents=True)
    (source / "guandan_marvel" / "__init__.py").write_text("", encoding="utf-8")
    native = source / "nanopokerlib" / "_guandan_cpp.cp312-win_amd64.pyd"
    native.write_bytes(b"native")
    files = []
    for index in range(120):
        relative = f"guandan_marvel/m{index}.py"
        target = source / relative
        target.write_bytes(f"# {index}\n".encode("utf-8"))
        raw = target.read_bytes()
        files.append({"path": relative, "bytes": len(raw),
                      "sha256": hashlib.sha256(raw).hexdigest()})
    checkpoint_bytes = b"checkpoint"
    digest = hashlib.sha256(checkpoint_bytes).hexdigest()
    monkeypatch.setattr(module, "EXPECTED_MARVEL_CHECKPOINT_SHA256", digest)
    monkeypatch.setattr(module, "EXPECTED_MARVEL_CHECKPOINT_BYTES", len(checkpoint_bytes))
    descriptor = {
        "checkpoint": {"sha256": digest},
        "runtime": {"native_filename": "_guandan_cpp.cp312-win_amd64.pyd",
                    "native_sha256": hashlib.sha256(b"native").hexdigest(),
                    "native_bytes": len(b"native")},
        "source_fingerprint": {
            "schema": "guandan-marvel-source-fingerprint-v1",
            "components": {"materialized-runtime": {"files": files, "hash": "0" * 64}},
            "hash": "0" * 64,
        },
    }
    bundle = runtime / "bundles" / "bundle"
    bundle.mkdir(parents=True)
    (bundle / "deployment.json").write_text(json.dumps(descriptor), encoding="utf-8")
    checkpoint = runtime / "checkpoint" / "checkpoint.pt"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(checkpoint_bytes)
    inputs = {"checkpoint": tmp_path / "checkpoint.pt", "bundle": tmp_path / "bundle"}
    return module, package, torch_root, inputs


def test_validate_marvel_package_without_static_libs_returns(tmp_path, monkeypatch):
    module, package, _torch_root, inputs = _marvel_package_with_static_lib_branch(
        tmp_path, monkeypatch)
    # No *.lib file: the guard must complete iteratively instead of raising
    # TypeError from iterating a Path.
    module._validate_marvel_package(package, inputs)


def test_validate_marvel_package_rejects_static_libs(tmp_path, monkeypatch):
    module, package, torch_root, inputs = _marvel_package_with_static_lib_branch(
        tmp_path, monkeypatch)
    (torch_root / "lib" / "static.lib").write_bytes(b"lib")
    with pytest.raises(RuntimeError, match="static torch libraries"):
        module._validate_marvel_package(package, inputs)


def test_spec_declares_vendored_torch_stdlib_hidden_imports():
    import ast
    tree = ast.parse((ROOT / "Guance.spec").read_text(encoding="utf-8"))
    values = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                getattr(target, "id", None) == "TARGET_HIDDEN_IMPORTS" for target in node.targets):
            values = ast.literal_eval(node.value)
    assert values is not None
    # Torch is vendored after PyInstaller analysis, so these stdlib modules it
    # imports at runtime must be declared explicitly or the frozen GPU worker
    # fails closed with MODEL_UNAVAILABLE.
    assert {"ctypes.util", "unittest.mock", "timeit", "heapq", "pickletools",
            "cProfile", "profile", "pstats", "colorsys"} <= set(values)


def test_default_profile_rejects_present_marvel_runtime(tmp_path):
    module = _build_release()
    root = tmp_path / "root"
    package = root / "release" / "Guance"
    (package / "_internal" / "marvel_runtime").mkdir(parents=True)
    (root / "src" / "guance" / "presentation" / "qml").mkdir(parents=True)
    (root / "data" / "profiles").mkdir(parents=True)
    with pytest.raises(RuntimeError):
        module._validate_package(root, package, capture_runtime=False)


def _fixed_summary(exe_sha: str, *, expect_device: str, device_fallback: bool) -> dict:
    return {
        "schema": "guance-gpu-first-fixed-input-v1",
        "provenance": "offline_confirmed_decision",
        "live_input_used": False, "recommendation_displayed": False,
        "requested_device": "cpu" if expect_device == "cpu" else "auto",
        "expect_device": expect_device,
        "worker_executable": "Guance.exe", "worker_executable_sha256": exe_sha,
        "package_resolved_assets": True,
        "runtime": {"actual_device": expect_device, "device_fallback": device_fallback,
                    "device_reason": "cuda_unavailable" if device_fallback else None,
                    "model_version": "MARVEL-V2-v1.3-iter002000", "model_sha256": "b" * 64},
        "load_seconds": 8.0, "provider_eligible_count": 34, "strict_eligible_count": 32,
        "strict_ineligible_count": 2, "recommendation_count": 32,
        "recommendation_error_count": 0, "model_fallback_count": 0,
        "device_fallback": device_fallback, "input_immutable": True,
        "gate": {"checkpoint_sha256": "b" * 64, "deployment_sha256": "c" * 64},
        "records": [{"index": index, "decision_identity": f"id-{index}", "kind": "pass"}
                    for index in range(32)],
        "ineligible": [{"phase": "x", "code": "y", "reason": "z"}] * 2,
    }


def test_merge_fixed_accepts_masked_device_fallback_and_rejects_it_elsewhere(tmp_path):
    """masked_cpu is an expected auto->CPU device fallback; the other modes are not.

    The product keeps model_fallback_count=0 in every mode.  An over-strict harness
    that demanded device_fallback=false everywhere failed a correct masked run.
    """
    import argparse
    import hashlib
    import sys

    sys.path.insert(0, str(ROOT))
    import scripts.run_final_local_gpu_acceptance as acc

    package = tmp_path / "Guance"
    package.mkdir()
    (package / "Guance.exe").write_bytes(b"exe")
    exe_sha = hashlib.sha256(b"exe").hexdigest()
    out = tmp_path / "evidence"
    out.mkdir()

    def build(masked_fallback: bool) -> None:
        for mode, spec in acc.FIXED_MODES.items():
            summary = _fixed_summary(exe_sha, expect_device=spec["expect"],
                                     device_fallback=masked_fallback if mode == "masked_cpu"
                                     else False)
            (out / spec["output"]).write_text(json.dumps(summary), encoding="utf-8")
            (out / f"fixed-mode-{mode}.json").write_text(json.dumps({
                "status": "passed", "exit": 0, "exe_sha256": exe_sha, "elapsed_s": 1.0,
                "progress": "p", "progress_summary": {"last_success_index": 32},
            }), encoding="utf-8")

    args = argparse.Namespace(package=package, out=out)
    build(masked_fallback=True)
    assert acc.cmd_merge_fixed(args) == 0
    report = json.loads((out / "fixed-matrix.json").read_text(encoding="utf-8"))
    assert report["status"] == "passed", report["failed_checks"]

    # The same masked fallback in gpu_auto must fail the expectation.
    build(masked_fallback=True)
    summary = _fixed_summary(exe_sha, expect_device="cuda:0", device_fallback=True)
    (out / acc.FIXED_MODES["gpu_auto"]["output"]).write_text(
        json.dumps(summary), encoding="utf-8")
    assert acc.cmd_merge_fixed(args) == 2
    report = json.loads((out / "fixed-matrix.json").read_text(encoding="utf-8"))
    assert "gpu_auto.device_fallback_semantics" in report["failed_checks"]
