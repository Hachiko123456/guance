"""Build and validate the portable Windows onedir package."""
from __future__ import annotations

import argparse
from hashlib import sha256
from pathlib import Path
import shutil
import subprocess
import sys
import time


def _tree_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(file.stat().st_size for file in path.rglob("*") if file.is_file())


def _remove_tree(path: Path, *, root: Path) -> None:
    """Remove only a known build child when --clean was explicitly requested."""

    resolved_root = root.resolve()
    resolved_path = path.resolve()
    if resolved_path == resolved_root or not resolved_path.is_relative_to(resolved_root):
        raise RuntimeError(f"refusing to remove path outside the project build root: {resolved_path}")
    if path.exists():
        shutil.rmtree(path)


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_inventory(root: Path) -> dict[str, int]:
    return {
        file.relative_to(root).as_posix(): file.stat().st_size
        for file in root.rglob("*")
        if file.is_file()
    }


def _require_same_inventory(source: Path, packaged: Path, *, label: str) -> None:
    expected = _file_inventory(source)
    actual = _file_inventory(packaged)
    missing = sorted(set(expected) - set(actual))
    mismatched = sorted(
        relative
        for relative in set(expected) & set(actual)
        if expected[relative] != actual[relative]
    )
    if missing or mismatched:
        details = [f"{label} validation failed"]
        if missing:
            details.append(f"missing={missing[:8]}")
        if mismatched:
            details.append(f"size_mismatch={mismatched[:8]}")
        raise RuntimeError("; ".join(details))


def _validate_package(root: Path) -> None:
    package = root / "release" / "Guance"
    internal = package / "_internal"
    required = [
        package / "Guance.exe",
        internal / "app.ico",
        internal / "data" / "profiles" / "tencent_daguandan" / "models" / "best.npz",
        internal / "guance" / "presentation" / "qml" / "Main.qml",
        internal / "guance" / "presentation" / "qml" / "SettingsPage.qml",
        internal / "PySide6" / "qml" / "QtQuick" / "qmldir",
        internal / "PySide6" / "qml" / "QtQuick" / "Controls" / "qmldir",
        internal / "PySide6" / "qml" / "QtQuick" / "Controls" / "qtquickcontrols2plugin.dll",
        internal / "PySide6" / "qml" / "QtQuick" / "Controls" / "Basic" / "qmldir",
        internal / "PySide6" / "qml" / "QtQuick" / "Controls" / "Basic" / "impl" / "qtquickcontrols2basicstyleimplplugin.dll",
        internal / "PySide6" / "qml" / "QtQuick" / "Controls" / "impl" / "qtquickcontrols2implplugin.dll",
        internal / "PySide6" / "qml" / "QtQuick" / "Templates" / "qtquicktemplates2plugin.dll",
        internal / "PySide6" / "qml" / "QtQuick" / "Layouts" / "qmldir",
        internal / "PySide6" / "qml" / "QtQuick" / "Window" / "qmldir",
        internal / "PySide6" / "qml" / "Qt" / "labs" / "platform" / "qmldir",
        internal / "PySide6" / "qml" / "QtQuick" / "Controls" / "Basic" / "qtquickcontrols2basicstyleplugin.dll",
        internal / "PySide6" / "qml" / "QtQuick" / "Layouts" / "qquicklayoutsplugin.dll",
        internal / "PySide6" / "qml" / "QtQuick" / "Window" / "quickwindowplugin.dll",
        internal / "PySide6" / "qml" / "Qt" / "labs" / "platform" / "labsplatformplugin.dll",
        internal / "runtime" / "mitmproxy" / "mitmdump.exe",
        internal / "runtime" / "mitmproxy" / "addon.py",
        internal / "runtime" / "mitmproxy" / "_internal",
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise RuntimeError(f"package validation failed; missing required files: {missing}")

    _require_same_inventory(
        root / "src" / "guance" / "presentation" / "qml",
        internal / "guance" / "presentation" / "qml",
        label="application QML",
    )
    _require_same_inventory(
        root / "runtime" / "mitmproxy",
        internal / "runtime" / "mitmproxy",
        label="mitmproxy frozen runtime",
    )
    model = root / "data" / "profiles" / "tencent_daguandan" / "models" / "best.npz"
    packaged_model = internal / "data" / "profiles" / "tencent_daguandan" / "models" / "best.npz"
    if _sha256(model) != _sha256(packaged_model):
        raise RuntimeError("package validation failed; packaged best.npz differs from the source model")

    # These are deliberate guardrails against accidentally reintroducing the
    # broad PySide6 collection that caused the original 800 MB package.
    forbidden = [
        internal / "PySide6" / "Qt6WebEngineCore.dll",
        internal / "PySide6" / "Qt6Quick3D.dll",
        internal / "PySide6" / "Qt6Designer.dll",
        internal / "PySide6" / "Qt6Charts.dll",
        internal / "PySide6" / "Qt6Multimedia.dll",
        internal / "PySide6" / "Qt6Pdf.dll",
        internal / "PySide6" / "qml" / "QtQuick3D",
        internal / "PySide6" / "qml" / "QtQuick" / "VirtualKeyboard",
        internal / "PySide6" / "qml" / "QtQuick" / "Controls" / "FluentWinUI3",
    ]
    present = [str(path) for path in forbidden if path.exists()]
    if present:
        raise RuntimeError(f"package validation failed; excluded Qt payload is present: {present}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--clean",
        action="store_true",
        help="remove only release/ and build/pyinstaller/ before rebuilding",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    dist = root / "release"
    work = root / "build" / "pyinstaller"
    model = root / "data" / "profiles" / "tencent_daguandan" / "models" / "best.npz"
    icon = root / "app.ico"
    qml = root / "src" / "guance" / "presentation" / "qml"
    runtime = root / "runtime" / "mitmproxy"
    runtime_hook = root / "scripts" / "qt_runtime_hook.py"

    for required in (model, icon, qml, runtime_hook):
        if not required.exists():
            raise SystemExit(f"required packaging input is missing: {required}")

    before_size = _tree_size(dist / "Guance")
    started = time.perf_counter()
    try:
        if args.clean:
            _remove_tree(dist, root=root)
            _remove_tree(work, root=root / "build")

        prepare = subprocess.run(
            [sys.executable, str(root / "scripts" / "prepare_runtime.py")],
            cwd=root,
            check=False,
        )
        if prepare.returncode:
            raise RuntimeError(f"runtime preparation failed with exit code {prepare.returncode}")

        command = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--distpath",
            str(dist),
            "--workpath",
            str(work),
        ]
        if args.clean:
            command.append("--clean")
        command.append(str(root / "Guance.spec"))

        build = subprocess.run(command, cwd=root, check=False)
        if build.returncode:
            raise RuntimeError(f"PyInstaller failed with exit code {build.returncode}")

        _validate_package(root)
    except (OSError, RuntimeError) as exc:
        elapsed = time.perf_counter() - started
        print(f"build failed after {elapsed:.1f}s: {exc}", file=sys.stderr)
        return 1

    elapsed = time.perf_counter() - started
    after_size = _tree_size(dist / "Guance")
    print(f"build complete: {dist / 'Guance'}")
    print(f"size: before={before_size:,} bytes after={after_size:,} bytes")
    print(f"elapsed: {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
