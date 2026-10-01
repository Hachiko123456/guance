"""Prepare the complete bundled mitmproxy helper for a local Windows build."""
from __future__ import annotations

import argparse
from pathlib import Path
import json
import shutil


def _metadata(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _source_files(source: Path) -> dict[Path, Path]:
    files = {Path("mitmdump.exe"): source / "mitmdump.exe"}
    files.update(
        {
            file.relative_to(source): file
            for file in (source / "_internal").rglob("*")
            if file.is_file()
        }
    )
    return files


def _matches(source: Path, destination: Path, addon: Path) -> bool:
    try:
        for relative, source_file in _source_files(source).items():
            if _metadata(source_file) != _metadata(destination / relative):
                return False
        return _metadata(addon) == _metadata(destination / "addon.py")
    except FileNotFoundError:
        return False


def _write_manifest(path: Path, source: Path, addon: Path) -> None:
    payload = {
        "source": str(source),
        "source_files": {
            relative.as_posix(): {
                "size": source_file.stat().st_size,
                "mtime_ns": source_file.stat().st_mtime_ns,
            }
            for relative, source_file in _source_files(source).items()
        },
        "addon": {
            "size": addon.stat().st_size,
            "mtime_ns": addon.stat().st_mtime_ns,
        },
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=Path(r"C:\Program Files\mitmproxy\bin"),
        help="mitmproxy standalone bin directory",
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=root / "runtime" / "mitmproxy",
        help="prepared runtime directory",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="recopy all source files even when the incremental check matches",
    )
    args = parser.parse_args()

    source = args.source.resolve()
    destination = args.destination.resolve()
    addon = root / "src" / "guance" / "infrastructure" / "capture" / "addon.py"
    helper = source / "mitmdump.exe"
    internal = source / "_internal"

    if not helper.is_file() or not internal.is_dir():
        raise SystemExit(f"mitmproxy standalone runtime not found under {source}")
    if not addon.is_file():
        raise SystemExit(f"Guance capture addon is missing: {addon}")

    destination.mkdir(parents=True, exist_ok=True)
    if not args.force and _matches(source, destination, addon):
        print(f"prepared {destination} (incremental; unchanged)")
        return 0

    copied = 0
    for relative, source_file in _source_files(source).items():
        destination_file = destination / relative
        destination_file.parent.mkdir(parents=True, exist_ok=True)
        if args.force or not destination_file.is_file() or _metadata(source_file) != _metadata(destination_file):
            shutil.copy2(source_file, destination_file)
            copied += 1

    destination_addon = destination / "addon.py"
    if args.force or not destination_addon.is_file() or _metadata(addon) != _metadata(destination_addon):
        shutil.copy2(addon, destination_addon)
        copied += 1

    # The manifest lives beside runtime/mitmproxy, so it is not included in
    # the shipped frozen runtime. Unknown existing files are intentionally
    # retained; this pass never deletes mitmproxy _internal files.
    _write_manifest(destination.parent / ".mitmproxy-runtime.json", source, addon)
    print(f"prepared {destination} ({copied} files copied; existing files retained)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
