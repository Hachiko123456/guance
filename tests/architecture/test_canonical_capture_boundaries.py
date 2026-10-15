from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_canonical_capture_producer_is_lazy_and_has_no_ui_or_network_imports():
    source = (ROOT / "src/guance/infrastructure/capture/canonical_capture.py").read_text(encoding="utf-8")
    assert "PySide" not in source and "Qt" not in source
    assert "import socket" not in source and "import requests" not in source and "import urllib" not in source
    assert "import torch" not in source.lower()
    assert "application.controller" not in source


def test_default_capture_package_does_not_import_canonical_producer():
    source = (ROOT / "src/guance/infrastructure/capture/__init__.py").read_text(encoding="utf-8")
    assert "canonical_capture" not in source


def test_explicit_producer_import_does_not_load_torch_or_native_runtime():
    code = r"""
import sys
from guance.infrastructure.capture.canonical_capture import CanonicalCaptureConverter
assert CanonicalCaptureConverter.producer == 'guance.CanonicalCaptureConverter'
blocked = [name for name in sys.modules if name == 'torch' or name.startswith('torch.')
           or name == 'nanopokerlib._guandan_cpp']
assert not blocked, blocked
"""
    completed = subprocess.run([sys.executable, "-I", "-B", "-c", code], cwd=ROOT,
                               capture_output=True, text=True,
                               env={"PYTHONPATH": str(ROOT / "src")}, timeout=30)
    # Isolated mode intentionally ignores PYTHONPATH; bootstrap via an explicit source insertion.
    if completed.returncode != 0:
        code = "import sys;sys.path.insert(0,r'%s');%s" % (str(ROOT / "src"), code)
        completed = subprocess.run([sys.executable, "-I", "-B", "-c", code], cwd=ROOT,
                                   capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr

def test_private_canonical_artifacts_remain_under_ignored_capture_root():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "/debug/captures/" in ignore
    source = (ROOT / "src/guance/infrastructure/capture/canonical_capture.py").read_text(encoding="utf-8")
    assert 'JOURNAL_FILENAME = "phase7_canonical_decisions.jsonl"' in source
    assert 'MANIFEST_FILENAME = "phase7_acceptance_input.json"' in source

def test_producer_never_accesses_client_payload_field_value():
    source = (ROOT / "src/guance/infrastructure/capture/canonical_capture.py").read_text(encoding="utf-8")
    assert 'record["payload_b64"]' not in source
    assert "base64" not in source
