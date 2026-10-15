from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "guance"
MARVEL = SRC / "infrastructure" / "model" / "marvel_v2"
PHASE4_MODULES = ("lifecycle_contracts.py", "history.py", "cache_lifecycle.py")


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            result.add("." * node.level + (node.module or ""))
    return result


def _fresh_python(code: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-B", "-c", code], cwd=ROOT, env=env,
        capture_output=True, text=True, check=False,
    )


def test_phase4_modules_are_pure_stdlib_protocol_layers():
    forbidden = (
        "torch", "numpy", "guandan", "_guandan_cpp", "guance.application",
        "guance.presentation", "guance.infrastructure.protocol",
        "guance.infrastructure.model.providers", ".checkpoint", ".runtime",
    )
    violations = []
    for name in PHASE4_MODULES:
        bad = sorted(item for item in _imports(MARVEL / name)
                     if item.startswith(forbidden))
        if bad:
            violations.append((name, bad))
    assert not violations


def test_phase4_production_has_no_fixture_upstream_or_file_io_dependency():
    forbidden_text = (
        "tests/", "tests\\", "d:\\project\\guandan", "guance_integration_handoff",
        "pathlib", "open(", "read_text(", "read_bytes(", "pickle", "torch.load",
    )
    violations = []
    for name in PHASE4_MODULES:
        text = (MARVEL / name).read_text(encoding="utf-8").lower()
        for needle in forbidden_text:
            if needle in text:
                violations.append((name, needle))
    assert not violations


def test_default_registry_and_fabledan_do_not_eagerly_load_phase4_modules():
    code = r'''
import sys
from guance.infrastructure.model.registry import default_registry
from guance.infrastructure.model.providers.fabledan import FableDanProvider
registry = default_registry()
assert registry._providers
forbidden = {
    "guance.infrastructure.model.marvel_v2.lifecycle_contracts",
    "guance.infrastructure.model.marvel_v2.history",
    "guance.infrastructure.model.marvel_v2.cache_lifecycle",
    "torch", "numpy", "guandan", "_guandan_cpp",
}
assert forbidden.isdisjoint(sys.modules), forbidden.intersection(sys.modules)
assert FableDanProvider.__name__ == "FableDanProvider"
'''
    result = _fresh_python(code)
    assert result.returncode == 0, result.stderr or result.stdout


def test_explicit_phase4_import_remains_lazy_and_does_not_load_model_runtime():
    code = r'''
import sys
from guance.infrastructure.model.marvel_v2 import (
    AbstractCacheLifecycle, ExactHistoryJournal, HistoryWorkspace,
)
blocked = [name for name in sys.modules
           if name.partition(".")[0] in {"torch", "numpy", "guandan", "_guandan_cpp"}]
assert not blocked, blocked
assert AbstractCacheLifecycle.__name__ == "AbstractCacheLifecycle"
assert ExactHistoryJournal.__name__ == "ExactHistoryJournal"
assert HistoryWorkspace.__name__ == "HistoryWorkspace"
'''
    result = _fresh_python(code)
    assert result.returncode == 0, result.stderr or result.stdout


def test_no_provider_worker_application_ui_live_or_runtime_phase4_consumer_exists():
    roots = (
        SRC / "application", SRC / "presentation", SRC / "infrastructure" / "protocol",
        SRC / "infrastructure" / "model" / "providers",
    )
    needles = ("marvel_v2.history", "marvel_v2.cache_lifecycle",
               "marvel_v2.lifecycle_contracts")
    hits = []
    for root in roots:
        for source in root.rglob("*.py"):
            text = source.read_text(encoding="utf-8")
            for needle in needles:
                if needle in text:
                    hits.append((str(source.relative_to(ROOT)), needle))
    for source in (MARVEL / "checkpoint.py", MARVEL / "runtime.py"):
        text = source.read_text(encoding="utf-8")
        for needle in needles:
            if needle in text:
                hits.append((str(source.relative_to(ROOT)), needle))
    assert not hits


def test_readiness_and_live_eligibility_remain_fail_closed():
    code = r'''
from guance.infrastructure.model.marvel_v2.identity import CheckpointIdentity
from guance.infrastructure.model.marvel_v2.encoder import adapt_live_state
from guance.infrastructure.model.marvel_v2 import UnsupportedStateError
identity = CheckpointIdentity()
assert identity.production_ready is False
try:
    adapt_live_state(object())
except UnsupportedStateError as exc:
    assert exc.code == "phase3_live_state_not_eligible"
else:
    raise AssertionError("live state unexpectedly eligible")
'''
    result = _fresh_python(code)
    assert result.returncode == 0, result.stderr or result.stdout
