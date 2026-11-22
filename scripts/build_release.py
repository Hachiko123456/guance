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

# Keep the builder and the frozen runtime on the same bundle and rules identities.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from guance.infrastructure.model.marvel_v2.rules_oracle import verified_rules_files
from guance.infrastructure.model.package_assets import (
    DANKS_BUNDLE_DIR_NAME, DANKS_RULES_DIR_NAME, DANKS_RUNTIME_DIR_NAME,
)


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
PROFILE_DEFAULT = "default"
PROFILE_MARVEL_GPU = "marvel-gpu"
PROFILE_DANKS_CPU = "danks-cpu"
MARVEL_SITE_ENV = "GUANCE_MARVEL_SITE_PACKAGES"
MARVEL_SOURCE_ENV = "GUANCE_MARVEL_SOURCE"
MARVEL_CHECKPOINT_ENV = "GUANCE_MARVEL_CHECKPOINT"
MARVEL_BUNDLE_ENV = "GUANCE_MARVEL_BUNDLE"
EXPECTED_MARVEL_CHECKPOINT_SHA256 = (
    "af0df1eaeb67c88e467ee78bf6b0494c49d000f5733d655ee81b40351d4ca9c8"
)
EXPECTED_MARVEL_CHECKPOINT_BYTES = 8588181
_TORCH_SKIP_DIRS = {"__pycache__", "bin", "include", "share", "test"}
_TORCH_SKIP_SUFFIXES = {".cmake", ".cpp", ".cuh", ".h", ".hpp", ".lib", ".pyc", ".pyi", ".pyo"}
# ``torch`` imports the top-level ``torchgen`` package unconditionally (for
# example through ``torch.utils._python_dispatch``) and imports ``sympy`` (and
# ``mpmath`` through sympy) from ``torch.fx.experimental.symbolic_shapes``.
# Omitting any of them makes the frozen worker report MODEL_UNAVAILABLE instead
# of loading.  ``numpy`` is supplied by the frozen application itself.  Keep
# this list explicit rather than copying the whole site-packages tree.
_TORCH_RUNTIME_PACKAGES = ("torch", "torchgen", "sympy", "mpmath")
_MARVEL_BUNDLE_FILES = ("deployment.json", "manifest.json", "SHA256SUMS")


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


_BUNDLE_PROVIDER_RUNTIMES = {
    "fabledan": ("numpy", None),
    "danks_v3": ("torch", 2),
}


def _validate_bundle_directory(bundle: Path, *, legacy: bool) -> list[Path]:
    """Strictly validate one bundle directory; return its allowed file set.

    ``legacy=True`` keeps the historical top-level FableDan/numpy-only
    contract; the catalog layout additionally accepts DanKS V3 torch bundles.
    """
    manifest_path = bundle / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"model bundle manifest is missing: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        artifact = manifest["artifact"]
        provider = manifest.get("provider")
        expected_runtime, expected_schema = _BUNDLE_PROVIDER_RUNTIMES.get(provider, (None, None))
        if (expected_runtime is None or manifest.get("runtime") != expected_runtime
                or not isinstance(artifact, str)):
            raise ValueError(f"only {sorted(_BUNDLE_PROVIDER_RUNTIMES)} bundles are allowed here")
        if legacy and provider != "fabledan":
            raise ValueError("only FableDan/numpy bundles are allowed in the default package")
        if expected_schema is not None and manifest.get("schema_version") != expected_schema:
            raise ValueError(f"{provider} bundle must declare schema_version={expected_schema}")
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
    return sorted(allowed)


def _model_bundle_files(root: Path) -> list[tuple[Path, Path]]:
    """Return explicit source/package file pairs for approved default bundles."""
    source_root = root / "data" / "models"
    if not source_root.is_dir():
        return []
    pairs: list[tuple[Path, Path]] = []
    for bundle in sorted(path for path in source_root.iterdir() if path.is_dir()):
        if bundle.name == "catalog":
            continue  # strict catalog layout, scanned below
        for path in _validate_bundle_directory(bundle, legacy=True):
            destination = Path("data/models") / bundle.name
            pairs.append((path, destination.joinpath(*path.relative_to(bundle).parts)))
    catalog_root = source_root / "catalog"
    if catalog_root.is_dir():
        for bundle in sorted(path for path in catalog_root.iterdir() if path.is_dir()):
            for path in _validate_bundle_directory(bundle, legacy=False):
                destination = Path("data/models/catalog") / bundle.name
                pairs.append((path, destination.joinpath(*path.relative_to(bundle).parts)))
    return pairs


def _marvel_inputs() -> dict[str, Path]:
    """Resolve and validate the explicit local-only MARVEL GPU build allowlist."""
    values = {
        "site_packages": os.environ.get(MARVEL_SITE_ENV, "").strip(),
        "source": os.environ.get(MARVEL_SOURCE_ENV, "").strip(),
        "checkpoint": os.environ.get(MARVEL_CHECKPOINT_ENV, "").strip(),
        "bundle": os.environ.get(MARVEL_BUNDLE_ENV, "").strip(),
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError(f"marvel-gpu profile requires explicit asset env vars: {missing}")
    resolved = {name: Path(value).resolve() for name, value in values.items()}
    for name in ("site_packages", "source", "bundle"):
        if not resolved[name].is_dir():
            raise RuntimeError(f"marvel-gpu asset directory is missing: {name}={resolved[name]}")
    if not resolved["checkpoint"].is_file():
        raise RuntimeError(f"marvel-gpu checkpoint is missing: {resolved['checkpoint']}")
    torch_root = resolved["site_packages"] / "torch"
    if not (torch_root / "__init__.py").is_file():
        raise RuntimeError(f"marvel-gpu site-packages does not contain torch: {torch_root}")
    _sha, size = _sha256_size(resolved["checkpoint"])
    if (_sha != EXPECTED_MARVEL_CHECKPOINT_SHA256 or size != EXPECTED_MARVEL_CHECKPOINT_BYTES):
        raise RuntimeError("marvel-gpu checkpoint identity does not match the accepted iter2000 artifact")
    descriptor = json.loads((resolved["bundle"] / "deployment.json").read_text(encoding="utf-8"))
    if descriptor.get("checkpoint", {}).get("sha256") != EXPECTED_MARVEL_CHECKPOINT_SHA256:
        raise RuntimeError("marvel-gpu descriptor does not pin the accepted iter2000 checkpoint")
    _verify_source_tree(resolved["source"], descriptor)
    native = resolved["source"] / "nanopokerlib" / descriptor["runtime"]["native_filename"]
    native_sha, native_bytes = _sha256_size(native)
    if (native_sha != descriptor["runtime"]["native_sha256"]
            or native_bytes != descriptor["runtime"]["native_bytes"]):
        raise RuntimeError("marvel-gpu native extension identity does not match the descriptor")
    expected_bundle = set(_MARVEL_BUNDLE_FILES)
    actual_bundle = {path.name for path in resolved["bundle"].iterdir() if path.is_file()}
    if actual_bundle != expected_bundle:
        raise RuntimeError(
            f"marvel-gpu descriptor bundle must contain exactly {sorted(expected_bundle)}; "
            f"found {sorted(actual_bundle)}"
        )
    for line in (resolved["bundle"] / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 2:
            raise RuntimeError("invalid marvel-gpu descriptor checksum entry")
        checksum, relative = parts[0], parts[1].lstrip("*")
        target = resolved["bundle"] / relative
        if not target.is_file() or _sha256(target) != checksum:
            raise RuntimeError(f"marvel-gpu descriptor checksum mismatch: {relative}")
    return resolved


def _sha256_size(path: Path) -> tuple[str, int]:
    digest = sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _verify_source_tree(source: Path, descriptor: dict) -> None:
    """Verify every descriptor-pinned source file exactly as the runtime will."""
    components = descriptor.get("source_fingerprint", {}).get("components", {})
    if not isinstance(components, dict) or not components:
        raise RuntimeError("marvel-gpu descriptor has no source fingerprint")
    seen = 0
    for component in components.values():
        for entry in component.get("files", []):
            relative = entry.get("path")
            if not isinstance(relative, str) or not relative:
                raise RuntimeError("invalid source fingerprint path")
            target = source.joinpath(*PurePosixPath(relative).parts)
            if not target.is_file():
                raise RuntimeError(f"marvel-gpu source file is missing: {relative}")
            raw = target.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            if (len(raw), sha256(raw).hexdigest()) != (entry.get("bytes"), entry.get("sha256")):
                raise RuntimeError(f"marvel-gpu source file identity mismatch: {relative}")
            seen += 1
    if seen < 100:
        raise RuntimeError(f"marvel-gpu source fingerprint is unexpectedly small: {seen}")


def _copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _install_marvel_runtime(package: Path, inputs: dict[str, Path]) -> dict[str, int]:
    """Copy the explicit local-only runtime allowlist into the frozen package."""
    internal = package / "_internal"
    runtime_root = internal / "marvel_runtime"
    site_target = runtime_root / "site-packages"
    copied = 0
    for package_name in _TORCH_RUNTIME_PACKAGES:
        package_source = inputs["site_packages"] / package_name
        if not (package_source / "__init__.py").is_file():
            raise RuntimeError(
                f"marvel-gpu site-packages does not contain required runtime package: {package_name}")
        package_target = site_target / package_name
        for path in sorted(package_source.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(package_source)
            if relative.parts and relative.parts[0] in _TORCH_SKIP_DIRS:
                continue
            if path.suffix.lower() in _TORCH_SKIP_SUFFIXES:
                continue
            _copy_file(path, package_target / relative)
            copied += 1
    typing_extensions = inputs["site_packages"] / "typing_extensions.py"
    if typing_extensions.is_file():
        _copy_file(typing_extensions, site_target / "typing_extensions.py")
    source_target = runtime_root / "source"
    if source_target.exists():
        shutil.rmtree(source_target)
    shutil.copytree(inputs["source"], source_target)
    checkpoint_target = runtime_root / "checkpoint" / inputs["checkpoint"].name
    _copy_file(inputs["checkpoint"], checkpoint_target)
    bundle_target = runtime_root / "bundles" / inputs["bundle"].name
    for name in _MARVEL_BUNDLE_FILES:
        _copy_file(inputs["bundle"] / name, bundle_target / name)
    return {
        "torch_files": copied,
        "source_files": sum(1 for path in source_target.rglob("*") if path.is_file()),
        "bundle_files": len(_MARVEL_BUNDLE_FILES),
    }


def _validate_marvel_package(package: Path, inputs: dict[str, Path]) -> None:
    internal = package / "_internal"
    runtime_root = internal / "marvel_runtime"
    torch_root = runtime_root / "site-packages" / "torch"
    required = [
        torch_root / "__init__.py",
        torch_root / "lib" / "torch_cuda.dll",
        torch_root / "lib" / "cudart64_12.dll",
        runtime_root / "site-packages" / "torchgen" / "__init__.py",
        runtime_root / "site-packages" / "sympy" / "__init__.py",
        runtime_root / "site-packages" / "mpmath" / "__init__.py",
        runtime_root / "source" / "nanopokerlib" / "_guandan_cpp.cp312-win_amd64.pyd",
        runtime_root / "source" / "guandan_marvel" / "__init__.py",
        runtime_root / "checkpoint" / inputs["checkpoint"].name,
        runtime_root / "bundles" / inputs["bundle"].name / "deployment.json",
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise RuntimeError(f"marvel-gpu package validation failed; missing: {missing}")
    if not any(torch_root.glob("_C*.pyd")):
        raise RuntimeError("marvel-gpu package validation failed; torch._C extension is missing")
    checkpoint = runtime_root / "checkpoint" / inputs["checkpoint"].name
    digest, size = _sha256_size(checkpoint)
    if (digest, size) != (EXPECTED_MARVEL_CHECKPOINT_SHA256, EXPECTED_MARVEL_CHECKPOINT_BYTES):
        raise RuntimeError("marvel-gpu package validation failed; checkpoint identity mismatch")
    descriptor = json.loads(
        (runtime_root / "bundles" / inputs["bundle"].name / "deployment.json").read_text(encoding="utf-8"))
    _verify_source_tree(runtime_root / "source", descriptor)
    if (internal / "runtime" / "mitmproxy").exists():
        raise RuntimeError("marvel-gpu package validation failed; capture runtime must be excluded")
    if list(package.rglob("python314.dll")):
        raise RuntimeError("marvel-gpu package validation failed; Python 3.14 payload is present")
    if list((torch_root / "lib").glob("*.lib")):
        raise RuntimeError("marvel-gpu package validation failed; static torch libraries are present")


def _danks_inputs(rules_source: Path | None) -> dict[str, Path]:
    """Resolve the local CPU Torch runtime allowlist for the danks-cpu profile.

    The DanKS V3 loader is strictly CPU (fail-closed device policy), so the
    current interpreter's own site-packages supply the trusted runtime; no
    CUDA payload and no MARVEL assets are required.
    """
    site_packages = Path(sys.executable).resolve().parent / "Lib" / "site-packages"
    if not (site_packages / "torch" / "__init__.py").is_file():
        raise RuntimeError(
            f"danks-cpu profile requires torch in the current interpreter: {site_packages}")
    if not list((site_packages / "torch").glob("_C*.pyd")):
        raise RuntimeError("danks-cpu profile validation failed; torch._C extension is missing")
    inputs = {"site_packages": site_packages}
    if rules_source is not None:
        rules_source = rules_source.resolve()
        verified_rules_files(rules_source)
        inputs["rules_source"] = rules_source
    return inputs


def _install_danks_runtime(package: Path, inputs: dict[str, Path]) -> dict[str, int]:
    """Copy the explicit CPU Torch allowlist into the frozen package."""
    internal = package / "_internal"
    runtime_root = internal / DANKS_RUNTIME_DIR_NAME
    site_target = runtime_root / "site-packages"
    copied = 0
    for package_name in _TORCH_RUNTIME_PACKAGES:
        package_source = inputs["site_packages"] / package_name
        if not (package_source / "__init__.py").is_file():
            raise RuntimeError(
                f"danks-cpu site-packages does not contain required runtime package: {package_name}")
        package_target = site_target / package_name
        for path in sorted(package_source.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(package_source)
            if relative.parts and relative.parts[0] in _TORCH_SKIP_DIRS:
                continue
            if path.suffix.lower() in _TORCH_SKIP_SUFFIXES:
                continue
            _copy_file(path, package_target / relative)
            copied += 1
    typing_extensions = inputs["site_packages"] / "typing_extensions.py"
    if typing_extensions.is_file():
        _copy_file(typing_extensions, site_target / "typing_extensions.py")
        copied += 1
    rules_source = inputs.get("rules_source")
    if rules_source is not None:
        for relative in verified_rules_files(rules_source):
            _copy_file(rules_source / relative, runtime_root / DANKS_RULES_DIR_NAME / relative)
            copied += 1
    return {"copied": copied}


def _validate_danks_package(package: Path, inputs: dict[str, Path]) -> None:
    internal = package / "_internal"
    runtime_root = internal / DANKS_RUNTIME_DIR_NAME
    torch_root = runtime_root / "site-packages" / "torch"
    required = [
        torch_root / "__init__.py",
        runtime_root / "site-packages" / "torchgen" / "__init__.py",
        runtime_root / "site-packages" / "sympy" / "__init__.py",
        runtime_root / "site-packages" / "mpmath" / "__init__.py",
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise RuntimeError(f"danks-cpu package validation failed; missing: {missing}")
    if not any(torch_root.glob("_C*.pyd")):
        raise RuntimeError("danks-cpu package validation failed; torch._C extension is missing")
    bundle = internal / "data" / "models" / "catalog" / DANKS_BUNDLE_DIR_NAME
    _validate_bundle_directory(bundle, legacy=False)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("provider") != "danks_v3":
        raise RuntimeError("danks-cpu package validation failed; default bundle must be DanKS V3")
    if (internal / "marvel_runtime").exists():
        raise RuntimeError("danks-cpu package validation failed; MARVEL runtime is present")
    helper = internal / "runtime" / "mitmproxy"
    if "rules_source" in inputs:
        rules_root = runtime_root / DANKS_RULES_DIR_NAME
        try:
            verified = set(verified_rules_files(rules_root))
        except ValueError as exc:
            raise RuntimeError(f"packaged DanKS rules validation failed: {exc}") from exc
        if set(_file_inventory(rules_root)) != {path.as_posix() for path in verified}:
            raise RuntimeError("packaged DanKS rules contain unexpected files")
    elif helper.exists():
        raise RuntimeError("danks-cpu capture runtime requires verified rules")
    if any(not path.is_relative_to(helper / "_internal") for path in package.rglob("python314.dll")):
        raise RuntimeError("danks-cpu package validation failed; Python 3.14 payload is present")
    if list((torch_root / "lib").glob("*.lib")):
        raise RuntimeError("danks-cpu package validation failed; static torch libraries are present")


def _validate_package(root: Path, package: Path | None = None, *, capture_runtime: bool = True,
                      marvel_inputs: dict[str, Path] | None = None,
                      danks_inputs: dict[str, Path] | None = None) -> None:
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
    # Expected destinations are relative to _internal (they carry the
    # data/models prefix); compare against the same base.
    actual_bundle_files = {path.relative_to(internal).as_posix(): path
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

    if marvel_inputs is not None:
        _validate_marvel_package(package, marvel_inputs)
    elif (internal / "marvel_runtime").exists():
        raise RuntimeError("package validation failed; MARVEL runtime is present in a non-GPU profile")
    if danks_inputs is not None:
        _validate_danks_package(package, danks_inputs)
    elif (internal / DANKS_RUNTIME_DIR_NAME).exists():
        raise RuntimeError("package validation failed; DanKS runtime is present in a non-danks profile")


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
    parser.add_argument(
        "--profile", choices=(PROFILE_DEFAULT, PROFILE_MARVEL_GPU, PROFILE_DANKS_CPU),
        default=PROFILE_DEFAULT,
        help="packaging profile; marvel-gpu adds the explicit local Torch/MARVEL allowlist, "
             "danks-cpu adds the local CPU Torch allowlist and the DanKS catalog bundle",
    )
    parser.add_argument("--danks-rules-source", type=Path,
                        help="verified GD-RULES source root for danks-cpu builds that include capture")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    marvel_inputs = None
    danks_inputs = None
    if args.danks_rules_source is not None and (args.profile != PROFILE_DANKS_CPU
                                               or args.exclude_capture_runtime):
        parser.error("--danks-rules-source is only used by danks-cpu with capture")
    if args.profile == PROFILE_MARVEL_GPU:
        if not args.exclude_capture_runtime:
            raise SystemExit("marvel-gpu profile requires --exclude-capture-runtime")
        try:
            marvel_inputs = _marvel_inputs()
        except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise SystemExit(f"marvel-gpu asset validation failed: {exc}") from None
    if args.profile == PROFILE_DANKS_CPU:
        if not args.exclude_capture_runtime and args.danks_rules_source is None:
            parser.error("danks-cpu with capture requires --danks-rules-source")
        try:
            danks_inputs = _danks_inputs(args.danks_rules_source)
        except (OSError, RuntimeError, ValueError) as exc:
            raise SystemExit(f"danks-cpu asset validation failed: {exc}") from None
    dist = (args.dist_dir if args.dist_dir is not None else root / "release").resolve()
    work_root = (args.work_dir if args.work_dir is not None else root / "build" / "pyinstaller").resolve()
    work = _mode_work_directory(
        work_root, exclude_capture_runtime=args.exclude_capture_runtime
    ).resolve()
    if args.profile != PROFILE_DEFAULT:
        work = (work_root / f"{_mode_work_directory(work_root, exclude_capture_runtime=args.exclude_capture_runtime).name}-{args.profile}").resolve()
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
        "profile": args.profile,
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

        if marvel_inputs is not None:
            installed = _install_marvel_runtime(dist / "Guance", marvel_inputs)
            print(json.dumps({"marvel_runtime_installed": installed}, sort_keys=True))
        if danks_inputs is not None:
            installed = _install_danks_runtime(dist / "Guance", danks_inputs)
            print(json.dumps({"danks_runtime_installed": installed}, sort_keys=True))
        _validate_package(
            root, dist / "Guance", capture_runtime=not args.exclude_capture_runtime,
            marvel_inputs=marvel_inputs, danks_inputs=danks_inputs,
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
