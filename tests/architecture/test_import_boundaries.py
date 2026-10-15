"""Static dependency and import-boundary contracts for ARCH-01/02/08/10/12."""
from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys

from guance.infrastructure.model.registry import default_registry

ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = ROOT / "src"
SRC = SRC_ROOT / "guance"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add("." * node.level + (node.module or ""))
    return found


def test_domain_does_not_import_infrastructure_or_presentation():
    violations = []
    for path in sorted((SRC / "domain").rglob("*.py")):
        bad = sorted(name for name in _imports(path)
                     if name.startswith("guance.infrastructure")
                     or name.startswith("guance.presentation")
                     or name.startswith("guance.application"))
        if bad:
            violations.append((str(path.relative_to(ROOT)), bad))
    assert not violations, violations


def test_application_model_service_depends_only_on_contracts_and_ports():
    imports = _imports(SRC / "application" / "model_service.py")
    forbidden = sorted(name for name in imports
                       if name.startswith("guance.infrastructure")
                       or name.startswith("guance.presentation")
                       or ".providers" in name
                       or name.endswith(".client")
                       or name.endswith(".worker"))
    assert not forbidden, forbidden
    assert "guance.contracts" in imports
    assert "guance.ports.model" in imports


def test_client_and_worker_share_codec_without_private_cross_imports():
    client_imports = _imports(SRC / "infrastructure" / "model" / "client.py")
    worker_imports = _imports(SRC / "infrastructure" / "model" / "worker.py")
    assert ".codec" in client_imports
    assert ".codec" in worker_imports
    assert not any(name.endswith(".worker") or name == "guance.infrastructure.model.worker"
                   for name in client_imports)
    assert not any(name.endswith(".client") or name == "guance.infrastructure.model.client"
                   for name in worker_imports)


def test_controller_does_not_import_concrete_model_provider():
    imports = _imports(SRC / "application" / "controller.py")
    assert not any(name.startswith("guance.infrastructure.model") or ".providers" in name
                   for name in imports)


def test_application_controller_does_not_import_infrastructure_adapters():
    imports = _imports(SRC / "application" / "controller.py")
    forbidden = sorted(name for name in imports if name.startswith("guance.infrastructure"))
    assert not forbidden, forbidden


def test_default_registry_contains_no_test_only_fake_provider():
    registry = default_registry()
    assert registry._providers
    assert not any("fake" in name.lower() or "synthetic" in name.lower()
                   for name in registry._providers)
    assert not any(getattr(factory, "test_only", False)
                   for factory in registry._providers.values())


def test_fabledan_only_import_requires_no_torch_or_onnx():
    code = r'''
import builtins
blocked = {"torch", "onnx", "onnxruntime"}
real_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.partition(".")[0] in blocked:
        raise AssertionError("heavy runtime imported: " + name)
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded
from guance.infrastructure.model.providers.fabledan import FableDanProvider
assert FableDanProvider.__name__ == "FableDanProvider"
'''
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT,
                               env=env, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr or completed.stdout
