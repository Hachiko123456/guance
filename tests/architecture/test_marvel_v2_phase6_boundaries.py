from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "guance"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            result.add("." * node.level + (node.module or ""))
    return result


def test_phase6_application_presentation_and_contracts_do_not_import_torch_or_native():
    roots = [SRC / "application", SRC / "presentation", SRC / "contracts.py", SRC / "ports"]
    forbidden = ("torch", "guandan_marvel", "nanopokerlib", "_guandan_cpp")
    hits = []
    for root in roots:
        paths = root.rglob("*.py") if root.is_dir() else (root,)
        for path in paths:
            for name in _imports(path):
                if name.startswith(forbidden):
                    hits.append((str(path.relative_to(ROOT)), name))
    assert not hits


def test_phase6_default_import_does_not_eagerly_import_optional_runtime():
    code = r'''
import sys
from guance.infrastructure.model.registry import default_registry
from guance.application.model_service import ModelService
registry = default_registry()
assert 'guandan_marvel_v2' in registry._providers
blocked = {
    'torch', 'guandan_marvel', 'nanopokerlib',
    'guance.infrastructure.model.marvel_v2.runtime',
    'guance.infrastructure.model.marvel_v2.checkpoint',
    'guance.infrastructure.model.marvel_v2.deployment_bundle',
}
assert blocked.isdisjoint(sys.modules), blocked.intersection(sys.modules)
'''
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT, env=env,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout


def test_phase6_provider_has_no_click_replay_or_traffic_mutation_surface():
    paths = [
        SRC / "infrastructure/model/providers/guandan_marvel_v2.py",
        SRC / "infrastructure/model/marvel_v2/deployment_bundle.py",
    ]
    forbidden = ("pyautogui", "click(", "request replay", "traffic modification",
                 "opponent_hands", "authorization", "cookie", "private key")
    hits = []
    for path in paths:
        text = path.read_text(encoding="utf-8").lower()
        for needle in forbidden:
            if needle in text:
                hits.append((str(path.relative_to(ROOT)), needle))
    assert not hits


def test_secure_source_bootstrap_isolated_before_fingerprint_validation():
    client = (SRC / "infrastructure/model/client.py").read_text(encoding="utf-8")
    deployment = (SRC / "infrastructure/model/marvel_v2/deployment_bundle.py").read_text(encoding="utf-8")
    assert '[sys.executable, "-I", "-X", "utf8", "-c"' in client
    assert 'env.pop("PYTHONPATH", None)' in client
    assert 'env.pop("PYTHONHOME", None)' in client
    assert 'python_paths.append' not in client
    assert 'sys.path.insert(0, text)' not in deployment
    assert '_verify_source_tree(source_root, descriptor.source_fingerprint)' in deployment
    verify_at = deployment.index('_verify_source_tree(source_root, descriptor.source_fingerprint)')
    bootstrap_at = deployment.index('bootstrap = _bootstrap_verified_packages(')
    assert verify_at < bootstrap_at
    assert 'class _VerifiedPackageFinder' in deployment
    assert 'raise ModuleNotFoundError(fullname)' in deployment
