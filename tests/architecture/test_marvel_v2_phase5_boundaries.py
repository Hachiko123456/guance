from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "guance"
MARVEL = SRC / "infrastructure" / "model" / "marvel_v2"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            result.add("." * node.level + (node.module or ""))
    return result


def test_phase5_identity_and_scalar_parity_modules_are_stdlib_only():
    forbidden = (
        "torch", "numpy", "guandan", "_guandan_cpp", "guance.application",
        "guance.presentation", "guance.infrastructure.protocol",
        "guance.infrastructure.model.providers", ".checkpoint", ".runtime",
    )
    for name in ("deployment.py", "parity.py"):
        assert not [x for x in _imports(MARVEL / name) if x.startswith(forbidden)]


def test_default_registry_does_not_eagerly_load_phase5_modules_or_optional_runtime():
    code = """
import sys
from guance.infrastructure.model.registry import default_registry
from guance.infrastructure.model.providers.fabledan import FableDanProvider
registry = default_registry()
assert registry._providers and FableDanProvider.__name__ == 'FableDanProvider'
blocked = {
    'guance.infrastructure.model.marvel_v2.deployment',
    'guance.infrastructure.model.marvel_v2.parity',
    'guance.infrastructure.model.marvel_v2.runtime',
    'torch', 'numpy', 'guandan', 'nanopokerlib',
}
assert blocked.isdisjoint(sys.modules), blocked.intersection(sys.modules)
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT,
                            env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout


def test_phase5_runtime_and_parity_stay_lazy_after_phase6_provider_wiring():
    roots = (SRC / "application", SRC / "presentation", SRC / "infrastructure" / "protocol")
    hits = []
    for root in roots:
        for path in root.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            for needle in ("marvel_v2.deployment", "marvel_v2.parity", "forward_synthetic_cached"):
                if needle in source:
                    hits.append((str(path.relative_to(ROOT)), needle))
    assert not hits
    provider = (SRC / "infrastructure/model/providers/guandan_marvel_v2.py").read_text(encoding="utf-8")
    assert "marvel_v2.deployment_bundle" in provider
    assert "from ..marvel_v2.runtime" in provider
    # Both imports are inside explicit load(), never at module import/default startup.
    tree = ast.parse(provider)
    top_imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    rendered = "\n".join(ast.unparse(node) for node in top_imports)
    assert "deployment_bundle" not in rendered and "marvel_v2.runtime" not in rendered


def test_fixed_tolerances_are_present_and_not_runtime_configurable():
    tolerances = ROOT / "build/acceptance/guandan-marvel-v2-phase5-20261013/fixed-tolerances.json"
    assert tolerances.is_file()
    source = tolerances.read_text(encoding="utf-8")
    assert '"fixed_before_implementation": true' in source
    assert '"rtol": 1e-05' in source or '"rtol": 0.00001' in source
