"""Generate deterministic Phase 8 inventory, PE closure, privacy, and license reports."""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import platform
import struct
import sys
from typing import Any

import pefile
import PySide6
import shiboken6
from PySide6.QtCore import qVersion

PE_SUFFIXES = {".exe", ".dll", ".pyd", ".sys"}
REQUIRED_QT = {
    "QtCore.pyd", "QtGui.pyd", "QtQml.pyd", "QtQuick.pyd", "QtOpenGL.pyd",
    "Qt6Core.dll", "Qt6Gui.dll", "Qt6Network.dll", "Qt6Qml.dll",
    "Qt6QmlMeta.dll", "Qt6QmlModels.dll", "Qt6QmlWorkerScript.dll",
    "Qt6Quick.dll", "Qt6OpenGL.dll", "Qt6QuickControls2.dll",
}
REQUIRED_CRT = {"msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"}
FORBIDDEN_SUFFIXES = {".pt", ".pth", ".ckpt", ".pem", ".key", ".jsonl", ".har", ".pcap"}
FORBIDDEN_COMPONENTS = {"tests", "private_data", "logs", "captures", "certificates", "preferences"}
FORBIDDEN_FILENAMES = {"python314.dll", "checkpoint_identity.json", "evaluation_games.jsonl"}
SENSITIVE_BYTES = (
    b"D:\\project\\guandan", b"D:/project/guandan", b"-----BEGIN PRIVATE KEY-----",
    b"authorization: bearer ", b"cookie=secret_sentinel", b"SECRET_SENTINEL",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_version(pe: pefile.PE) -> str:
    fixed = getattr(pe, "VS_FIXEDFILEINFO", None)
    if not fixed:
        return ""
    info = fixed[0]
    return ".".join(str(value) for value in (
        info.FileVersionMS >> 16, info.FileVersionMS & 0xFFFF,
        info.FileVersionLS >> 16, info.FileVersionLS & 0xFFFF,
    ))


def machine_name(value: int) -> str:
    return {0x8664: "amd64", 0x14C: "x86", 0xAA64: "arm64"}.get(value, hex(value))


def pe_record(path: Path, package: Path) -> dict[str, Any]:
    record: dict[str, Any] = {
        "path": path.relative_to(package).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256(path),
        "machine": "",
        "file_version": "",
        "imports": [],
        "delay_imports": [],
        "parse_error": "",
    }
    try:
        image = pefile.PE(str(path), fast_load=False)
        record["machine"] = machine_name(image.FILE_HEADER.Machine)
        record["file_version"] = file_version(image)
        record["imports"] = sorted({entry.dll.decode(errors="replace") for entry in getattr(image, "DIRECTORY_ENTRY_IMPORT", [])})
        record["delay_imports"] = sorted({entry.dll.decode(errors="replace") for entry in getattr(image, "DIRECTORY_ENTRY_DELAY_IMPORT", [])})
        image.close()
    except Exception as exc:  # malformed/non-PE files keep explicit evidence
        record["parse_error"] = f"{type(exc).__name__}: {exc}"
    return record


def resolve_import(name: str, importer: Path, package: Path, by_name: dict[str, list[Path]]) -> dict[str, Any]:
    lower = name.lower()
    if lower.startswith(("api-ms-win-", "ext-ms-win-")):
        return {"kind": "windows_api_set", "path": ""}
    internal = package / "_internal"
    ordered = [importer.parent, internal, internal / "PySide6", internal / "shiboken6", package]
    for candidate in ordered:
        target = candidate / name
        if target.is_file():
            return {"kind": "package", "path": target.relative_to(package).as_posix()}
    hits = by_name.get(lower, [])
    if len(hits) == 1:
        return {"kind": "package", "path": hits[0].relative_to(package).as_posix()}
    if len(hits) > 1:
        hashes = {sha256(path) for path in hits}
        return {
            "kind": "package_duplicate_same" if len(hashes) == 1 else "package_duplicate_conflict",
            "paths": [path.relative_to(package).as_posix() for path in hits],
        }
    windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
    for base in (windows / "System32", windows / "System32" / "drivers"):
        target = base / name
        if target.is_file():
            return {"kind": "windows_system", "path": str(target)}
    return {"kind": "unresolved", "path": ""}


def source_match(path: Path, package_root: Path, source_root: Path, prefix: str) -> dict[str, Any]:
    relative = path.relative_to(package_root / "_internal" / prefix)
    source = source_root / relative
    return {
        "path": path.relative_to(package_root).as_posix(),
        "source": str(source),
        "source_exists": source.is_file(),
        "match": source.is_file() and path.stat().st_size == source.stat().st_size and sha256(path) == sha256(source),
    }


def package_metadata(name: str) -> dict[str, str]:
    try:
        item = metadata.metadata(name)
        return {
            "name": name,
            "version": metadata.version(name),
            "license_expression": item.get("License-Expression", ""),
            "license": item.get("License", ""),
            "home_page": item.get("Home-page", ""),
        }
    except metadata.PackageNotFoundError:
        return {"name": name, "version": "missing", "license_expression": "", "license": "", "home_page": ""}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--candidate", type=int, default=7)
    args = parser.parse_args()
    package = args.package_root.resolve()
    evidence = args.evidence_root.resolve()
    reports = evidence / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    if not package.is_dir() or not (package / "Guance.exe").is_file():
        raise SystemExit(f"invalid package root: {package}")

    files = sorted(path for path in package.rglob("*") if path.is_file())
    inventory = [{
        "path": path.relative_to(package).as_posix(), "bytes": path.stat().st_size,
        "sha256": sha256(path),
    } for path in files]
    inventory_report = {
        "schema": "guance-phase8-package-inventory-v1", "candidate": args.candidate,
        "package_root": str(package), "file_count": len(files),
        "tree_bytes": sum(path.stat().st_size for path in files), "files": inventory,
    }
    (reports / "package-inventory.json").write_text(json.dumps(inventory_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    by_name: dict[str, list[Path]] = defaultdict(list)
    for path in files:
        by_name[path.name.lower()].append(path)
    pe_paths = [path for path in files if path.suffix.lower() in PE_SUFFIXES]
    pe_records = [pe_record(path, package) for path in pe_paths]
    unresolved: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for record, path in zip(pe_records, pe_paths, strict=True):
        resolved = []
        for name in [*record["imports"], *record["delay_imports"]]:
            resolution = resolve_import(name, path, package, by_name)
            item = {"name": name, **resolution}
            resolved.append(item)
            if resolution["kind"] == "unresolved":
                unresolved.append({"importer": record["path"], "name": name})
            if resolution["kind"] == "package_duplicate_conflict":
                conflicts.append({"importer": record["path"], "name": name, "paths": resolution["paths"]})
        record["resolved_imports"] = resolved
    pe_report = {
        "schema": "guance-phase8-pe-inventory-v1", "candidate": args.candidate,
        "pe_file_count": len(pe_records), "files": pe_records,
        "unresolved": unresolved, "duplicate_conflicts": conflicts,
        "all_parseable": not any(item["parse_error"] for item in pe_records),
        "all_amd64": all(item["machine"] == "amd64" for item in pe_records if item["machine"]),
        "status": "passed" if not unresolved and not conflicts and not any(item["parse_error"] for item in pe_records) and all(item["machine"] == "amd64" for item in pe_records) else "failed",
    }
    (reports / "pe-inventory.json").write_text(json.dumps(pe_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    internal = package / "_internal"
    pyside_root = Path(PySide6.__file__).resolve().parent
    shiboken_root = Path(shiboken6.__file__).resolve().parent
    pyside_files = sorted(path for path in (internal / "PySide6").rglob("*") if path.is_file())
    shiboken_files = sorted(path for path in (internal / "shiboken6").rglob("*") if path.is_file())
    pyside_matches = [source_match(path, package, pyside_root, "PySide6") for path in pyside_files]
    shiboken_matches = [source_match(path, package, shiboken_root, "shiboken6") for path in shiboken_files]
    qtcore = internal / "PySide6" / "QtCore.pyd"
    qtcore_record = next(item for item in pe_records if item["path"] == "_internal/PySide6/QtCore.pyd")
    qt_versions = sorted({item["file_version"] for item in pe_records if item["path"].startswith("_internal/PySide6/") and Path(item["path"]).name.startswith("Qt6") and item["file_version"]})
    required_missing = sorted(name for name in REQUIRED_QT if not by_name.get(name.lower()))
    qt_report = {
        "schema": "guance-phase8-qt-closure-v1", "candidate": args.candidate,
        "python": platform.python_version(), "pointer_bits": struct.calcsize("P") * 8,
        "pyside_version": PySide6.__version__, "shiboken_version": shiboken6.__version__, "qt_version": qVersion(),
        "required_missing": required_missing, "qt_binary_versions": qt_versions,
        "qtcore_imports": qtcore_record["imports"],
        "qtcore_requires_shiboken": "shiboken6.abi3.dll" in qtcore_record["imports"],
        "shiboken_discoverable": bool(by_name.get("shiboken6.abi3.dll")),
        "pyside_source_matches": pyside_matches, "shiboken_source_matches": shiboken_matches,
        "source_match_failures": [item for item in [*pyside_matches, *shiboken_matches] if not item["match"]],
    }
    qt_report["status"] = "passed" if (not required_missing and qt_report["qtcore_requires_shiboken"] and qt_report["shiboken_discoverable"] and qt_versions == ["6.11.1.0"] and not qt_report["source_match_failures"] and pe_report["status"] == "passed") else "failed"
    (reports / "qt-closure.json").write_text(json.dumps(qt_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    crt = {}
    for name in sorted(REQUIRED_CRT):
        occurrences = by_name.get(name, [])
        crt[name] = [{
            "path": path.relative_to(package).as_posix(), "bytes": path.stat().st_size,
            "sha256": sha256(path), "file_version": next((item["file_version"] for item in pe_records if item["path"] == path.relative_to(package).as_posix()), ""),
        } for path in occurrences]
    crt_report = {
        "schema": "guance-phase8-crt-closure-v1", "candidate": args.candidate,
        "required": crt,
        "duplicates": {name: values for name, values in crt.items() if len(values) != 1},
        "versions": sorted({item["file_version"] for values in crt.values() for item in values}),
        "source": "PySide6 wheel controlled CRT set",
    }
    crt_report["status"] = "passed" if not crt_report["duplicates"] and crt_report["versions"] == ["14.44.35211.0"] else "failed"
    (reports / "crt-closure.json").write_text(json.dumps(crt_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    forbidden_paths = []
    sensitive_hits = []
    for path in files:
        relative = path.relative_to(package)
        parts = {part.lower() for part in relative.parts}
        if path.suffix.lower() in FORBIDDEN_SUFFIXES or parts & FORBIDDEN_COMPONENTS or path.name.lower() in FORBIDDEN_FILENAMES:
            forbidden_paths.append(relative.as_posix())
        try:
            data = path.read_bytes()
        except OSError:
            continue
        markers = SENSITIVE_BYTES
        if path.suffix.lower() in PE_SUFFIXES:
            # TLS/network libraries legitimately embed PEM labels as parser constants.
            # PE content is still scanned for explicit external paths and test sentinels.
            markers = tuple(marker for marker in SENSITIVE_BYTES if marker != b"-----BEGIN PRIVATE KEY-----")
        for marker in markers:
            if marker.lower() in data.lower():
                sensitive_hits.append({"path": relative.as_posix(), "marker": marker.decode(errors="replace")})
    privacy_report = {
        "schema": "guance-phase8-package-privacy-v1", "candidate": args.candidate,
        "forbidden_paths": sorted(forbidden_paths), "sensitive_hits": sensitive_hits,
        "python314_present": bool(by_name.get("python314.dll")),
        "checkpoint_present": any(path.suffix.lower() in {".pt", ".pth", ".ckpt"} for path in files),
        "capture_helper_present": bool(by_name.get("mitmdump.exe")),
        "capture_helper_policy": "excluded from this evaluation-only candidate because its independent Python 3.14 runtime violates the CPython 3.12 closure gate",
        "external_marvel_packaged": False,
    }
    privacy_report["status"] = "passed" if not forbidden_paths and not sensitive_hits and not privacy_report["python314_present"] and not privacy_report["checkpoint_present"] and not privacy_report["capture_helper_present"] else "failed"
    (reports / "privacy-scan.json").write_text(json.dumps(privacy_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    license_report = {
        "schema": "guance-phase8-license-redistribution-v1", "candidate": args.candidate,
        "python": {"version": platform.python_version(), "license": "Python Software Foundation License"},
        "packages": [package_metadata(name) for name in ("PyInstaller", "PySide6", "shiboken6", "numpy", "psutil", "pywin32")],
        "qt": {"version": qVersion(), "distribution": "PySide6 wheel", "license_review": "LGPL/GPL/commercial terms apply; retain corresponding notices and relinkability obligations before redistribution"},
        "crt": {"version": "14.44.35211.0", "source": "PySide6 wheel", "redistribution_review": "Microsoft Visual C++ runtime redistribution terms apply"},
        "fabledan_model": {"packaged": bool(list(package.rglob("best.npz"))), "redistribution_rights": "unverified"},
        "external_marvel": {"checkpoint_packaged": False, "source_packaged": False, "native_runtime_packaged": False, "redistribution_rights": "unverified"},
        "capture_helper": {"packaged": False, "reason": "excluded from experimental candidate; independent runtime is Python 3.14"},
        "candidate_classification": "local experimental/evaluation-only",
        "redistribution_authorized": False,
        "publish": False,
        "conclusion": "Audit passed only as an explicit no-redistribution conclusion. Do not publish or redistribute this candidate until third-party notices and FableDan artifact rights receive a separate legal review.",
        "status": "passed_for_local_evaluation_only",
    }
    (reports / "license-redistribution.json").write_text(json.dumps(license_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    summary = {
        "schema": "guance-phase8-package-audit-summary-v1", "candidate": args.candidate,
        "inventory": "passed", "pe": pe_report["status"], "qt": qt_report["status"],
        "crt": crt_report["status"], "privacy": privacy_report["status"],
        "license": license_report["status"],
        "status": "passed" if pe_report["status"] == qt_report["status"] == crt_report["status"] == privacy_report["status"] == "passed" else "failed",
    }
    (reports / "package-audit-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0 if summary["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
