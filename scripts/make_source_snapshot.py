"""Create and verify a local-only source recovery snapshot for offline delivery.

The snapshot is intentionally allowlisted. Tracked files are included, ignored and
build outputs are excluded, and only explicitly named untracked files may be added.
No git index/ref/working-tree mutation happens here: the tracked changes are
exported as a read-only patch file.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Iterable
import zipfile

ZIP_DATE = (2026, 10, 20, 0, 0, 0)

# Never copy these even if a future .gitignore change makes them untracked.
EXCLUDED_PREFIXES = (
    ".git/", ".claude/", ".codex/", ".opencode/", ".idea/", ".vscode/",
    ".workbuddy/", ".pytest_cache/", "build/", "release/", "release_local/",
    "runtime/", "logs/", "debug/captures/", "private_data/", "prototypes/",
    "pythonProject/", "docs/agent/",
)
EXCLUDED_NAMES = {
    "opencode.json", "=810", ".env", "preferences.json", "proxy-lease.json",
}
EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".npz", ".pt", ".pth", ".ckpt", ".onnx",
                     ".safetensors", ".pem", ".p12", ".pfx", ".key", ".cer", ".crt",
                     ".der", ".secret", ".mitm", ".pcap", ".pcapng", ".dmp", ".log")

# Untracked files that are part of the verified candidate but have not been
# committed yet. Globbed patterns only; broad "everything" globs are refused.
UNTRACKED_ALLOWLIST = (
    "CLAUDE.md",
    "docs/GPU_INFERENCE.md",
    "docs/PROJECT_STATUS.md",
    "docs/OFFLINE_DELIVERY.md",
    "docs/THIRD_PARTY_AND_REDISTRIBUTION.md",
    "docs/examples/*.json",
    "scripts/*.py",
    "src/guance/infrastructure/model/*.py",
    "src/guance/infrastructure/model/marvel_v2/*.py",
    "tests/**/*.py",
    "tests/**/*.json",
)


class SnapshotError(RuntimeError):
    pass


def _sha256_bytes(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, check=False,
    )
    if result.returncode:
        raise SnapshotError(f"git {' '.join(args)} failed: {result.stderr.decode(errors='replace')}")
    return result.stdout.decode("utf-8", errors="replace")


def _git_optional(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, check=False,
    )
    return result.stdout.decode("utf-8", errors="replace").strip() if not result.returncode else ""


def _is_excluded(relative: str) -> bool:
    posix = PurePosixPath(relative).as_posix()
    if posix in EXCLUDED_NAMES or PurePosixPath(posix).name in EXCLUDED_NAMES:
        return True
    if any(posix.startswith(prefix) for prefix in EXCLUDED_PREFIXES):
        return True
    return posix.lower().endswith(EXCLUDED_SUFFIXES)


def _untracked_allowed(relative: str) -> bool:
    from fnmatch import fnmatch
    posix = PurePosixPath(relative).as_posix()
    return any(fnmatch(posix, pattern) for pattern in UNTRACKED_ALLOWLIST)


def _tracked(root: Path) -> list[str]:
    raw = _git(root, "ls-files", "-z")
    return sorted(item for item in raw.split("\x00") if item)


def _untracked(root: Path) -> list[str]:
    raw = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
    return sorted(item for item in raw.split("\x00") if item)


def collect(root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return (included file records, classification report)."""
    tracked = _tracked(root)
    untracked = _untracked(root)
    records: list[dict[str, Any]] = []
    excluded_tracked: list[str] = []
    excluded_untracked: list[str] = []
    for relative in tracked:
        if _is_excluded(relative):
            excluded_tracked.append(relative)
            continue
        path = root / relative
        if not path.is_file():
            raise SnapshotError(f"tracked path is not a regular file: {relative}")
        data = path.read_bytes()
        records.append({
            "path": PurePosixPath(relative).as_posix(), "bytes": len(data),
            "sha256": _sha256_bytes(data), "classification": "tracked",
        })
    included_untracked = 0
    for relative in untracked:
        if _is_excluded(relative) or not _untracked_allowed(relative):
            excluded_untracked.append(relative)
            continue
        path = root / relative
        if not path.is_file():
            continue
        data = path.read_bytes()
        records.append({
            "path": PurePosixPath(relative).as_posix(), "bytes": len(data),
            "sha256": _sha256_bytes(data), "classification": "untracked-allowlisted",
        })
        included_untracked += 1
    records.sort(key=lambda item: item["path"])
    names = [item["path"] for item in records]
    if len(names) != len(set(names)):
        raise SnapshotError("duplicate path in snapshot")
    report = {
        "tracked_total": len(tracked),
        "untracked_total": len(untracked),
        "included": len(records),
        "included_untracked": included_untracked,
        "excluded_tracked": excluded_tracked,
        "excluded_untracked": excluded_untracked,
        "allowlist": list(UNTRACKED_ALLOWLIST),
    }
    return records, report


def write_snapshot(root: Path, output: Path) -> dict[str, Any]:
    records, report = collect(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp_zip = output.with_suffix(output.suffix + ".tmp")
    for record in records:
        data = (root / record["path"]).read_bytes()
        if _sha256_bytes(data) != record["sha256"]:
            raise SnapshotError(f"source changed while snapshotting: {record['path']}")
    with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for record in records:
            info = zipfile.ZipInfo(record["path"], date_time=ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, (root / record["path"]).read_bytes())
    os.replace(tmp_zip, output)

    patch = _git(root, "diff", "--binary", "HEAD").encode("utf-8")
    patch_path = output.with_name("tracked-uncommitted.patch")
    patch_path.write_bytes(patch)
    manifest = {
        "schema": "guance-offline-source-snapshot-v1",
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "repository": str(root),
        "git": {
            "head": _git(root, "rev-parse", "HEAD").strip(),
            "branch": _git(root, "branch", "--show-current").strip(),
            "upstream": _git_optional(
                root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"),
            "remote_freshness": "UNVERIFIED (no network fetch performed)",
        },
        "zip": {
            "path": output.name, "bytes": output.stat().st_size,
            "sha256": _sha256_bytes(output.read_bytes()),
        },
        "tracked_patch": {
            "path": patch_path.name, "bytes": len(patch), "sha256": _sha256_bytes(patch),
        },
        "classification": report,
        "files": records,
    }
    manifest_path = output.with_name("source-manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    return manifest


def _safe_members(archive: zipfile.ZipFile) -> list[str]:
    names = archive.namelist()
    for name in names:
        pure = PurePosixPath(name)
        if pure.is_absolute() or ".." in pure.parts or "\\" in name:
            raise SnapshotError(f"unsafe zip member: {name}")
    if len(names) != len(set(names)):
        raise SnapshotError("duplicate zip member")
    return names


def verify_snapshot(zip_path: Path, python: Path, check_root: Path) -> dict[str, Any]:
    zip_path = zip_path.resolve()
    check_root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        names = _safe_members(archive)
        archive.extractall(check_root)
        for name in names:
            if _is_excluded(name):
                raise SnapshotError(f"excluded path present in snapshot: {name}")
    required = [
        "src/guance/__main__.py",
        "src/guance/infrastructure/model/catalog.py",
        "src/guance/infrastructure/model/device_policy.py",
        "src/guance/infrastructure/model/selection.py",
        "src/guance/infrastructure/model/marvel_v2/decision_request.py",
        "src/guance/infrastructure/model/marvel_v2/recommendation.py",
        "src/guance/infrastructure/model/marvel_v2/trick_replay.py",
        "scripts/build_release.py",
        "scripts/package_model_bundle.py",
        "Guance.spec",
        "docs/PROJECT_STATUS.md",
        "docs/OFFLINE_DELIVERY.md",
        "docs/GPU_INFERENCE.md",
    ]
    missing = [name for name in required if not (check_root / name).is_file()]
    if missing:
        raise SnapshotError(f"snapshot is missing required files: {missing}")
    probe = (
        "import sys\n"
        "sys.path.insert(0, 'src')\n"
        "import guance\n"
        "import guance.config\n"
        "import guance.contracts\n"
        "import guance.infrastructure.model.catalog\n"
        "import guance.infrastructure.model.device_policy\n"
        "import guance.infrastructure.model.selection\n"
        "import guance.infrastructure.model.marvel_v2.decision_request\n"
        "import guance.infrastructure.model.marvel_v2.recommendation\n"
        "import guance.infrastructure.model.marvel_v2.trick_replay\n"
        "assert 'torch' not in sys.modules\n"
        "print('IMPORT_OK', guance.__version__)\n"
    )
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["GUANCE_DATA_DIR"] = str(check_root / ".isolated-data")
    result = subprocess.run(
        [str(python), "-B", "-I", "-c", probe], cwd=check_root, env=env,
        capture_output=True, text=True, timeout=120, check=False,
    )
    pyc = [str(path) for path in check_root.rglob("*.pyc")]
    if result.returncode or "IMPORT_OK" not in result.stdout:
        raise SnapshotError(
            f"snapshot import probe failed exit={result.returncode}: "
            f"{(result.stderr or result.stdout)[-800:]}"
        )
    return {
        "zip": str(zip_path),
        "zip_sha256": _sha256_bytes(zip_path.read_bytes()),
        "members": len(names),
        "required_present": True,
        "import_probe": "passed",
        "import_probe_python": str(python),
        "pyc_written": pyc,
        "excluded_paths_in_zip": 0,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verify-python", type=Path, default=None)
    parser.add_argument("--verify-dir", type=Path, default=None)
    parser.add_argument("--verify-report", type=Path, default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)
    root = args.root.resolve()
    manifest = write_snapshot(root, args.output.resolve())
    print(json.dumps({
        "zip": manifest["zip"], "files": len(manifest["files"]),
        "included_untracked": manifest["classification"]["included_untracked"],
    }, sort_keys=True))
    if args.verify_python is not None:
        verify_dir = (args.verify_dir or Path(tempfile.mkdtemp(prefix="guance-src-verify-"))).resolve()
        if verify_dir.exists():
            shutil.rmtree(verify_dir)
        report = verify_snapshot(args.output.resolve(), args.verify_python.resolve(), verify_dir)
        if args.verify_report is not None:
            args.verify_report.parent.mkdir(parents=True, exist_ok=True)
            args.verify_report.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
