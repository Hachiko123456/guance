"""Build and validate the portable Windows onedir package."""
from __future__ import annotations

import argparse
import json
import os
from hashlib import sha256
from pathlib import Path, PurePosixPath
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


def _validate_output_paths(root: Path, dist: Path, work: Path, *, custom: bool) -> None:
    """Keep custom clean/build outputs inside build/acceptance and non-overlapping."""
    for selected, label in ((dist, "dist"), (work, "work")):
        if selected == root or not selected.is_relative_to(root):
            raise RuntimeError(f"{label} directory must be a child of the repository: {selected}")
    if custom:
        acceptance = (root / "build" / "acceptance").resolve()
        for selected, label in ((dist, "dist"), (work, "work")):
            if selected == acceptance or not selected.is_relative_to(acceptance):
                raise RuntimeError(
                    f"custom {label} directory must stay inside build/acceptance: {selected}"
                )
    if dist == work or dist.is_relative_to(work) or work.is_relative_to(dist):
        raise RuntimeError("dist and work directories must not overlap")


CAPTURE_RUNTIME_ENV = "GUANCE_EXPERIMENTAL_PACKAGE_NO_CAPTURE_RUNTIME"


def _build_identity(*, exclude_capture_runtime: bool) -> str:
    return "evaluation-no-capture" if exclude_capture_runtime else "development-with-capture"


def _mode_work_directory(work: Path, *, exclude_capture_runtime: bool) -> Path:
    """Use separate PyInstaller caches for capture-included and no-capture builds."""
    return work / _build_identity(exclude_capture_runtime=exclude_capture_runtime)


def _validate_packaging_inputs(root: Path, *, exclude_capture_runtime: bool) -> None:
    """Validate only inputs used by the selected package identity."""
    required = [
        root / "app.ico",
        root / "src" / "guance" / "presentation" / "qml",
        root / "scripts" / "qt_runtime_hook.py",
    ]
    if not exclude_capture_runtime:
        runtime = root / "runtime" / "mitmproxy"
        required.extend((
            runtime,
            runtime / "mitmdump.exe",
            runtime / "_internal",
            runtime / "addon.py",
        ))
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise RuntimeError(f"required packaging input is missing: {missing}")


def _build_environment(*, exclude_capture_runtime: bool) -> dict[str, str]:
    """Return an explicit environment so build modes cannot inherit each other."""
    env = os.environ.copy()
    env.pop(CAPTURE_RUNTIME_ENV, None)
    if exclude_capture_runtime:
        env[CAPTURE_RUNTIME_ENV] = "1"
        windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
        python_root = Path(sys.executable).resolve().parent
        env["PATH"] = os.pathsep.join(str(path) for path in (
            python_root, python_root / "Scripts", windows / "System32", windows,
            windows / "System32" / "Wbem",
        ) if path.exists())
    return env


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_inventory(root: Path) -> dict[str, tuple[int, str]]:
    return {
        file.relative_to(root).as_posix(): (file.stat().st_size, _sha256(file))
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


def _model_bundle_files(root: Path) -> list[tuple[Path, Path]]:
    """Return explicit source/package file pairs for approved default bundles."""
    source_root = root / "data" / "models"
    if not source_root.is_dir():
        return []
    pairs: list[tuple[Path, Path]] = []
    for bundle in sorted(path for path in source_root.iterdir() if path.is_dir()):
        manifest_path = bundle / "manifest.json"
        if not manifest_path.is_file():
            raise RuntimeError(f"model bundle manifest is missing: {manifest_path}")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            artifact = manifest["artifact"]
            if (manifest.get("provider") != "fabledan" or manifest.get("runtime") != "numpy"
                    or not isinstance(artifact, str)):
                raise ValueError("only FableDan/numpy bundles are allowed in the default package")
            relative = Path(*PurePosixPath(artifact).parts)
            if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
                raise ValueError("invalid artifact path")
            artifact_path = (bundle / relative).resolve()
            if not artifact_path.is_relative_to(bundle.resolve()) or not artifact_path.is_file():
                raise ValueError("artifact is missing or escapes bundle")
            digest = _sha256(artifact_path)
            if manifest.get("artifact_sha256") != digest:
                raise ValueError("artifact hash does not match manifest")
        except (OSError, UnicodeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invalid default model bundle {manifest_path}: {exc}") from None
        allowed = {manifest_path.resolve(), artifact_path}
        sums = bundle / "SHA256SUMS"
        if sums.exists():
            if not sums.is_file():
                raise RuntimeError(f"invalid model checksum file: {sums}")
            seen: set[str] = set()
            for line in sums.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                parts = line.split()
                if len(parts) != 2 or len(parts[0]) != 64 or any(c not in "0123456789abcdef" for c in parts[0]):
                    raise RuntimeError(f"invalid model checksum entry: {sums}")
                checksum_path = PurePosixPath(parts[1].lstrip("*"))
                if checksum_path.is_absolute() or any(part in {"", ".", ".."} for part in checksum_path.parts):
                    raise RuntimeError(f"invalid model checksum path: {sums}")
                key = checksum_path.as_posix()
                if key in seen:
                    raise RuntimeError(f"duplicate model checksum entry: {sums}")
                seen.add(key)
                target = (bundle / Path(*checksum_path.parts)).resolve()
                if (not target.is_relative_to(bundle.resolve()) or not target.is_file()
                        or _sha256(target) != parts[0]):
                    raise RuntimeError(f"model checksum mismatch: {sums}")
            if PurePosixPath(artifact).as_posix() not in seen or manifest.get("artifact_sha256") != digest:
                raise RuntimeError(f"model checksum does not cover artifact: {sums}")
            allowed.add(sums.resolve())
        extras = [path for path in bundle.rglob("*") if path.is_file() and path.resolve() not in allowed]
        if extras:
            raise RuntimeError(f"unexpected files in model bundle: {[str(path) for path in extras[:4]]}")
        destination = Path("data/models") / bundle.name
        pairs.extend((path, destination / path.name if path.parent == bundle else
                      destination.joinpath(*path.relative_to(bundle).parts))
                     for path in sorted(allowed) if path.is_file())
    return pairs


def _validate_package(root: Path, package: Path | None = None, *, capture_runtime: bool = True) -> None:
    package = package if package is not None else root / "release" / "Guance"
    internal = package / "_internal"
    required = [
        package / "Guance.exe",
        internal / "app.ico",
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
        *([
            internal / "runtime" / "mitmproxy" / "mitmdump.exe",
            internal / "runtime" / "mitmproxy" / "addon.py",
            internal / "runtime" / "mitmproxy" / "_internal",
        ] if capture_runtime else []),
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise RuntimeError(f"package validation failed; missing required files: {missing}")

    _require_same_inventory(
        root / "src" / "guance" / "presentation" / "qml",
        internal / "guance" / "presentation" / "qml",
        label="application QML",
    )
    if capture_runtime:
        _require_same_inventory(
            root / "runtime" / "mitmproxy",
            internal / "runtime" / "mitmproxy",
            label="mitmproxy frozen runtime",
        )
    elif (internal / "runtime" / "mitmproxy").exists():
        raise RuntimeError("package validation failed; excluded capture runtime is present")
    model = root / "data" / "profiles" / "tencent_daguandan" / "models" / "best.npz"
    packaged_model = internal / "data" / "profiles" / "tencent_daguandan" / "models" / "best.npz"
    if model.is_file() and (not packaged_model.is_file() or _sha256(model) != _sha256(packaged_model)):
        raise RuntimeError("package validation failed; packaged legacy model differs from the source model")

    expected_bundles = _model_bundle_files(root)
    actual_bundle_root = internal / "data" / "models"
    actual_bundle_files = {path.relative_to(actual_bundle_root).as_posix(): path
                           for path in actual_bundle_root.rglob("*") if path.is_file()} if actual_bundle_root.is_dir() else {}
    expected_bundle_names = {destination.as_posix().replace("\\", "/"): source
                             for source, destination in expected_bundles}
    if set(actual_bundle_files) != set(expected_bundle_names):
        raise RuntimeError("package validation failed; model bundle contains undeclared or missing files")
    for relative, source in expected_bundle_names.items():
        packaged = actual_bundle_files[relative]
        if _sha256(source) != _sha256(packaged):
            raise RuntimeError(f"package validation failed; model bundle file differs: {relative}")

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
        help="remove only the selected dist/work directories before rebuilding",
    )
    parser.add_argument(
        "--dist-dir", type=Path, default=None,
        help="PyInstaller dist root (defaults to repository release/)",
    )
    parser.add_argument(
        "--work-dir", type=Path, default=None,
        help="PyInstaller analysis/work root (defaults to build/pyinstaller/)",
    )
    parser.add_argument(
        "--exclude-capture-runtime", action="store_true",
        help="build an evaluation-only package without the independently frozen capture helper",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    dist = (args.dist_dir if args.dist_dir is not None else root / "release").resolve()
    work_root = (args.work_dir if args.work_dir is not None else root / "build" / "pyinstaller").resolve()
    work = _mode_work_directory(
        work_root, exclude_capture_runtime=args.exclude_capture_runtime
    ).resolve()
    try:
        _validate_output_paths(
            root, dist, work,
            custom=args.dist_dir is not None or args.work_dir is not None,
        )
        if args.exclude_capture_runtime:
            _validate_packaging_inputs(root, exclude_capture_runtime=True)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None

    identity = _build_identity(exclude_capture_runtime=args.exclude_capture_runtime)
    print(json.dumps({
        "build_identity": identity,
        "capture_runtime": "excluded" if args.exclude_capture_runtime else "included",
        "dist": str(dist),
        "work": str(work),
    }, sort_keys=True))

    before_size = _tree_size(dist / "Guance")
    started = time.perf_counter()
    try:
        if args.clean:
            _remove_tree(dist, root=root)
            _remove_tree(work, root=root)

        if not args.exclude_capture_runtime:
            prepare = subprocess.run(
                [sys.executable, str(root / "scripts" / "prepare_runtime.py")],
                cwd=root,
                check=False,
            )
            if prepare.returncode:
                raise RuntimeError(f"runtime preparation failed with exit code {prepare.returncode}")
            _validate_packaging_inputs(root, exclude_capture_runtime=False)

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

        build_env = _build_environment(
            exclude_capture_runtime=args.exclude_capture_runtime
        )
        build = subprocess.run(command, cwd=root, env=build_env, check=False)
        if build.returncode:
            raise RuntimeError(f"PyInstaller failed with exit code {build.returncode}")

        _validate_package(
            root, dist / "Guance", capture_runtime=not args.exclude_capture_runtime
        )
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
