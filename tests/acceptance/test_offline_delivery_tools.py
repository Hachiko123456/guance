"""Offline-delivery tooling tests: snapshot allowlist, launcher, capability matrix.

These tests target real gaps rather than string presence: they prove excluded
private/build paths never enter the source snapshot, the launcher cannot take an
executable from a bundle, the child environment does not mutate PATH, and the
capability matrix cannot infer frozen GPU support from a passing no-capture
package.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import scripts.make_source_snapshot as snapshot  # noqa: E402
import scripts.run_local_inference_check as launcher  # noqa: E402
import scripts.run_offline_delivery_acceptance as delivery  # noqa: E402


@pytest.mark.parametrize("path", [
    ".claude/settings.json",
    ".codex/config.toml",
    ".opencode/state.json",
    "opencode.json",
    "=810",
    "prototypes/demo.qml",
    "build/acceptance/x.json",
    "release/Guance/Guance.exe",
    "runtime/mitmproxy/mitmdump.exe",
    "private_data/capture.jsonl",
    "docs/agent/STATE.md",
    "data/profiles/tencent_daguandan/models/best.npz",
    "debug/captures/20261014-143204/frames.jsonl",
    "src/guance/__pycache__/x.pyc",
    "logs/app.log",
    "config/preferences.json",
])
def test_snapshot_excludes_private_build_and_secret_paths(path: str) -> None:
    assert snapshot._is_excluded(path)


@pytest.mark.parametrize("path", [
    "src/guance/bootstrap.py",
    "README.md",
    "docs/BUILD.md",
    "tests/model/conftest.py",
    "Guance.spec",
    "data/profiles/tencent_daguandan/models/README.md",
])
def test_snapshot_keeps_normal_source_paths(path: str) -> None:
    assert not snapshot._is_excluded(path)


def test_untracked_allowlist_covers_required_candidate_files() -> None:
    required = [
        "CLAUDE.md",
        "docs/GPU_INFERENCE.md",
        "src/guance/infrastructure/model/catalog.py",
        "src/guance/infrastructure/model/device_policy.py",
        "src/guance/infrastructure/model/selection.py",
        "src/guance/infrastructure/model/marvel_v2/decision_request.py",
        "src/guance/infrastructure/model/marvel_v2/recommendation.py",
        "src/guance/infrastructure/model/marvel_v2/trick_replay.py",
        "scripts/package_model_bundle.py",
        "tests/model/test_catalog.py",
        "tests/acceptance/test_phase6_package_tool.py",
    ]
    for path in required:
        assert snapshot._untracked_allowed(path), path
    for path in ("opencode.json", ".claude/settings.json", "prototypes/x.qml",
                 "src/guance/application/controller.py"):
        assert not snapshot._untracked_allowed(path), path


def _git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"],
                   check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "test"], check=True)
    (root / "src" / "guance" / "infrastructure" / "model").mkdir(parents=True)
    (root / "src" / "guance" / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    (root / "src" / "guance" / "infrastructure" / "model" / "tracked.py").write_text(
        "x = 1\n", encoding="utf-8")
    (root / ".gitignore").write_text("build/\n.claude/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "src", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "base"], check=True)
    (root / "src" / "guance" / "infrastructure" / "model" / "new_untracked.py").write_text(
        "y = 2\n", encoding="utf-8")
    (root / "random.txt").write_text("noise\n", encoding="utf-8")
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    (root / "build").mkdir()
    (root / "build" / "artifact.bin").write_bytes(b"x")
    return root


def test_snapshot_collect_includes_only_allowlisted_untracked(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    records, report = snapshot.collect(root)
    paths = {item["path"] for item in records}
    assert "src/guance/tracked.py" in paths
    assert "src/guance/infrastructure/model/new_untracked.py" in paths
    assert "random.txt" not in paths
    assert ".claude/settings.json" not in paths
    assert "build/artifact.bin" not in paths
    assert report["included_untracked"] == 1


def test_snapshot_zip_contains_no_excluded_paths(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    output = tmp_path / "out" / "snapshot.zip"
    manifest = snapshot.write_snapshot(root, output)
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
    assert "src/guance/infrastructure/model/new_untracked.py" in names
    assert not any(snapshot._is_excluded(name) for name in names)
    assert manifest["zip"]["sha256"] == snapshot._sha256_bytes(output.read_bytes())
    assert {item["path"] for item in manifest["files"]} == set(names)


def test_snapshot_verifier_rejects_zip_traversal(tmp_path: Path) -> None:
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as archive:
        archive.writestr("../evil.py", "x")
    with zipfile.ZipFile(bad) as archive:
        with pytest.raises(snapshot.SnapshotError):
            snapshot._safe_members(archive)


def test_launcher_rejects_relative_and_bundle_interpreter(tmp_path: Path) -> None:
    with pytest.raises(launcher.LaunchError):
        launcher.validate_interpreter(Path("python.exe"))
    real = tmp_path / "python.exe"
    real.write_bytes(b"")
    assert launcher.validate_interpreter(real) == real.resolve()
    forbidden = ROOT / "build" / "acceptance" / "fake" / "python.exe"
    with pytest.raises(launcher.LaunchError):
        launcher.validate_interpreter(forbidden)
    non_exe = tmp_path / "python.txt"
    non_exe.write_text("x", encoding="utf-8")
    with pytest.raises(launcher.LaunchError):
        launcher.validate_interpreter(non_exe)


def test_launcher_command_uses_explicit_python_and_mode_bundle(tmp_path: Path) -> None:
    python = tmp_path / "python.exe"
    python.write_bytes(b"")
    output = tmp_path / "out.json"
    gpu = launcher.build_command("gpu-auto", python, output)
    assert gpu[0] == str(python)
    assert "--device" in gpu and gpu[gpu.index("--device") + 1] == "auto"
    assert str(launcher.GPU_BUNDLES) in " ".join(gpu)
    hidden = launcher.build_command("no-cuda-hidden", python, output)
    assert hidden[3] == "no-gpu"
    assert "--device" not in hidden
    cpu = launcher.build_command("cpu-only-auto", python, output)
    assert str(launcher.CPU_BUNDLES) in " ".join(cpu)
    with pytest.raises(launcher.LaunchError):
        launcher.build_command("unknown", python, output)


def test_launcher_child_env_is_isolated_and_keeps_path(tmp_path: Path) -> None:
    env = launcher.child_env("gpu-auto", tmp_path)
    assert env["GUANCE_DATA_DIR"] == str(tmp_path.resolve())
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["PATH"] == os.environ.get("PATH")
    if "CUDA_VISIBLE_DEVICES" not in os.environ:
        assert "CUDA_VISIBLE_DEVICES" not in env
    hidden = launcher.child_env("no-cuda-hidden", tmp_path)
    assert hidden["CUDA_VISIBLE_DEVICES"] == ""


def test_worker_envelope_is_protocol_three_with_full_identity() -> None:
    envelope = delivery._envelope("load", "req-1", source="x")
    assert envelope["protocol_version"] == 3
    for name in delivery.IDENTITY_FIELDS:
        assert name in envelope
    assert envelope["type"] == "load"


def test_capability_matrix_never_infers_frozen_gpu_from_missing_facts() -> None:
    entries = delivery.capability_entries({"source_check": {}, "package": {}})
    by_name = {entry["capability"]: entry["status"] for entry in entries}
    assert by_name["source_gpu_auto"] == "UNVERIFIED"
    assert by_name["fresh_frozen_no_capture"] == "UNVERIFIED"
    assert by_name["frozen_marvel_gpu"] == "NOT_IMPLEMENTED"
    assert by_name["frozen_gpu_self_contained"] == "NOT_IMPLEMENTED"
    assert by_name["redistribution"] == "NOT AUTHORIZED"
    assert by_name["capture"] == "UNSUPPORTED"


def test_capability_matrix_passes_forward_only_measured_capabilities() -> None:
    entries = delivery.capability_entries({
        "source_check": {"gpu-auto": "passed", "cpu-only-auto": "passed",
                         "forced-cpu": "passed", "cpu-only-fail-closed": "passed"},
        "package": {"static_audit": "passed", "gui_smoke": "passed",
                    "frozen_worker": "passed"},
    })
    by_name = {entry["capability"]: entry["status"] for entry in entries}
    assert by_name["source_gpu_auto"] == "PASSED"
    assert by_name["source_cpu"] == "PASSED"
    assert by_name["fresh_frozen_no_capture"] == "PASSED"
    assert by_name["frozen_marvel_gpu"] == "NOT_IMPLEMENTED"
    assert by_name["frozen_gpu_self_contained"] == "NOT_IMPLEMENTED"
    assert by_name["live_ui"] == "UNVERIFIED"


def test_public_docs_do_not_depend_on_private_agent_docs() -> None:
    for relative in delivery.PUBLIC_DOCS:
        path = ROOT / relative
        assert path.is_file(), relative
        text = path.read_text(encoding="utf-8")
        assert "docs/agent" not in text, relative
        assert ".claude/" not in text, relative
        assert ".codex/" not in text, relative


def test_inventory_reports_relative_paths_and_hashes(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b.txt").write_text("hello", encoding="utf-8")
    items = delivery.inventory(tmp_path)
    assert items == [{"path": "a/b.txt", "bytes": 5,
                      "sha256": delivery.sha256(tmp_path / "a" / "b.txt")}]


def test_runtime_hook_enforces_utf8_on_ansi_stdio(tmp_path: Path) -> None:
    """Frozen windowed stdio defaults to ANSI; the worker wire contract is UTF-8."""
    hook = ROOT / "scripts" / "qt_runtime_hook.py"
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import io, runpy, sys\n"
        "raw = io.BytesIO()\n"
        "sys.stdout = io.TextIOWrapper(raw, encoding='cp936', errors='strict', write_through=True)\n"
        "sys.stderr = io.TextIOWrapper(io.BytesIO(), encoding='cp936', errors='strict')\n"
        f"runpy.run_path({str(hook)!r}, run_name='__main__')\n"
        "assert sys.stdout.encoding.replace('-', '').lower() == 'utf8', sys.stdout.encoding\n"
        "sys.stdout.write('\\u5355\\u5f20')\n"
        "sys.stdout.flush()\n"
        "assert raw.getvalue() == '\\u5355\\u5f20'.encode('utf-8'), raw.getvalue()\n"
        "sys.__stdout__.write('HOOK_UTF8_OK\\n')\n"
        "sys.__stdout__.flush()\n",
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, "-B", str(probe)], cwd=tmp_path,
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    assert "HOOK_UTF8_OK" in result.stdout
