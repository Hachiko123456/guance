"""Phase 8 package-script safety and helper coverage."""
import ast
import os
from pathlib import Path
import sys

import pytest

from scripts.build_release import (
    CAPTURE_RUNTIME_ENV,
    _build_environment,
    _mode_work_directory,
    _validate_output_paths,
    _validate_packaging_inputs,
    main,
)


def test_phase8_custom_outputs_must_stay_in_acceptance_and_not_overlap(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    dist = root / "build" / "acceptance" / "phase8" / "package"
    work = root / "build" / "acceptance" / "phase8" / "work"
    _validate_output_paths(root.resolve(), dist.resolve(), work.resolve(), custom=True)

    with pytest.raises(RuntimeError, match="build/acceptance"):
        _validate_output_paths(root.resolve(), (root / "src").resolve(), work.resolve(), custom=True)
    with pytest.raises(RuntimeError, match="must not overlap"):
        _validate_output_paths(root.resolve(), dist.resolve(), (dist / "nested").resolve(), custom=True)


def _minimal_packaging_source(root: Path) -> None:
    (root / "src" / "guance" / "presentation" / "qml").mkdir(parents=True)
    (root / "scripts").mkdir(parents=True)
    (root / "app.ico").write_bytes(b"ico")
    (root / "scripts" / "qt_runtime_hook.py").write_text("# hook\n", encoding="utf-8")


def test_no_capture_input_validation_never_requires_prepared_runtime(tmp_path: Path) -> None:
    root = tmp_path / "source"
    _minimal_packaging_source(root)

    _validate_packaging_inputs(root, exclude_capture_runtime=True)
    assert not (root / "runtime" / "mitmproxy").exists()

    with pytest.raises(RuntimeError, match="mitmproxy"):
        _validate_packaging_inputs(root, exclude_capture_runtime=False)


def test_danks_capture_build_requires_verified_rules_input(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["build_release.py", "--profile", "danks-cpu"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert "danks-cpu with capture requires --danks-rules-source" in capsys.readouterr().err


def test_capture_build_modes_have_distinct_cache_and_explicit_environment(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = tmp_path / "work"
    assert _mode_work_directory(work, exclude_capture_runtime=True) != _mode_work_directory(
        work, exclude_capture_runtime=False
    )

    monkeypatch.setenv(CAPTURE_RUNTIME_ENV, "inherited-invalid-value")
    excluded = _build_environment(exclude_capture_runtime=True)
    included = _build_environment(exclude_capture_runtime=False)
    assert excluded[CAPTURE_RUNTIME_ENV] == "1"
    assert CAPTURE_RUNTIME_ENV not in included
    assert excluded is not os.environ


def test_spec_guards_all_preflight_capture_runtime_checks() -> None:
    source = (Path(__file__).resolve().parents[2] / "Guance.spec").read_text(encoding="utf-8")
    tree = ast.parse(source)
    guarded_checks = []
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = ast.unparse(node.test)
        body = ast.get_source_segment(source, node) or ""
        if test == "not EXCLUDE_CAPTURE_RUNTIME" and "mitmdump.exe" in body:
            guarded_checks.append(body)
    assert len(guarded_checks) == 1
    guard = guarded_checks[0]
    assert "MITMPROXY_RUNTIME / 'mitmdump.exe'" in guard or 'MITMPROXY_RUNTIME / "mitmdump.exe"' in guard
    assert "MITMPROXY_RUNTIME / '_internal'" in guard or 'MITMPROXY_RUNTIME / "_internal"' in guard
    assert "MITMPROXY_RUNTIME / 'addon.py'" in guard or 'MITMPROXY_RUNTIME / "addon.py"' in guard
