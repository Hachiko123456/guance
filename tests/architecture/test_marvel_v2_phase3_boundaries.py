from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "guance"
PHASE3 = SRC / "infrastructure" / "model" / "marvel_v2"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            result.add("." * node.level + (node.module or ""))
    return result


def test_phase3_modules_are_pure_protocol_and_do_not_import_runtime_layers():
    forbidden_prefixes = (
        "torch", "numpy", "guandan", "_guandan_cpp", "guance.application",
        "guance.presentation", "guance.infrastructure.protocol",
        "guance.infrastructure.model.providers", "guance.infrastructure.model.runtime",
    )
    violations = []
    for name in ("contracts.py", "encoder.py", "event_encoder.py",
                 "action_decoder.py", "action_protocol.py"):
        imports = _imports(PHASE3 / name)
        bad = sorted(item for item in imports if item.startswith(forbidden_prefixes))
        if bad:
            violations.append((name, bad))
    assert not violations


def test_phase3_production_does_not_read_test_or_upstream_fixtures():
    violations = []
    for name in ("contracts.py", "encoder.py", "event_encoder.py",
                 "action_decoder.py", "action_protocol.py"):
        text = (PHASE3 / name).read_text(encoding="utf-8").lower()
        for needle in ("tests/", "tests\\", "d:\\project\\guandan", "guance_integration_handoff"):
            if needle in text:
                violations.append((name, needle))
    assert not violations


def test_default_and_fabledan_imports_do_not_load_phase3_heavy_or_native_modules():
    code = r'''
import sys
import guance
from guance.infrastructure.model.providers.fabledan import FableDanProvider
from guance.infrastructure.model.marvel_v2 import encode_channels, generate_legal_rows
blocked = [name for name in sys.modules if name.partition(".")[0] in {"torch", "guandan_marvel", "_guandan_cpp"}]
assert not blocked, blocked
assert callable(encode_channels) and callable(generate_legal_rows)
assert FableDanProvider.__name__ == "FableDanProvider"
'''
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT,
                            env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout


def test_no_application_protocol_or_presentation_phase3_consumer_exists():
    roots = (SRC / "application", SRC / "presentation", SRC / "infrastructure" / "protocol")
    hits = []
    for root in roots:
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "marvel_v2.encoder" in text or "marvel_v2.action_protocol" in text or "marvel_v2.event_encoder" in text:
                hits.append(str(path.relative_to(ROOT)))
    assert not hits


def test_default_registry_does_not_import_phase3_encoder_or_action_modules():
    code = r'''
import sys
from guance.infrastructure.model.registry import default_registry
registry = default_registry()
assert registry._providers
forbidden = {
    "guance.infrastructure.model.marvel_v2.encoder",
    "guance.infrastructure.model.marvel_v2.event_encoder",
    "guance.infrastructure.model.marvel_v2.action_decoder",
    "guance.infrastructure.model.marvel_v2.action_protocol",
    "guance.infrastructure.model.marvel_v2.contracts",
}
assert forbidden.isdisjoint(sys.modules), forbidden.intersection(sys.modules)
'''
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT,
                            env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout
