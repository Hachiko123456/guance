"""Real NTFS junction helper for Windows reparse tests (no monkeypatching)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


def make_junction(link: Path, target: Path) -> None:
    """Create a real directory junction. Non-Windows hosts skip explicitly."""
    if sys.platform != "win32":
        pytest.skip("NTFS junction creation is Windows-only; no fake substitute is used")
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise AssertionError(
            f"mklink /J failed on this Windows host: {completed.stdout} {completed.stderr}")


def make_file_symlink(link: Path, target: Path) -> None:
    """Create a real file symlink; explicit skip when the host lacks the privilege."""
    import os
    try:
        os.symlink(target, link)
    except OSError as exc:
        pytest.skip(f"file symlink creation unavailable on this host: {exc}")


__all__ = ["make_file_symlink", "make_junction"]
