"""Strict catalog discovery over an explicit root's direct bundle subdirectories.

Discovery is data-driven: every bundle is validated from its own manifest v2,
artifact bytes/SHA-256/SHA256SUMS and the selected provider's family contract.
Discovered never means active; nothing is selected by iteration, mtime or name.
"""
from __future__ import annotations

from pathlib import Path

from guance.ports.model import (
    ActiveSelectionDTO, CatalogEntryDTO, CatalogProblemDTO, CatalogScanDTO,
)
from .errors import ModelBackendError
from .manifest import MAX_MANIFEST_BYTES, ModelBundle, ModelManifest, strict_json
from .registry import ProviderRegistry, default_registry
from .selection import (
    ActiveStateSnapshot, commit_active_selection, is_reparse_point, load_active_selection,
    restore_active_state, safe_bundle_directory_name, save_active_selection,
    stage_active_selection,
)

CATALOG_BUNDLE_INVALID = "CATALOG_BUNDLE_INVALID"
CATALOG_DUPLICATE_MODEL_ID = "DUPLICATE_MODEL_ID"
CATALOG_ROOT_INVALID = "CATALOG_ROOT_INVALID"
MANIFEST_FILE_NAME = "manifest.json"
CHECKSUMS_FILE_NAME = "SHA256SUMS"


def _reject_catalog_reparse(path: Path) -> None:
    """Reject a symlink/junction/reparse point on the catalog root or any existing parent."""
    for candidate in reversed([path, *path.parents]):
        if not candidate.exists():
            continue
        if candidate.is_symlink() or is_reparse_point(candidate):
            raise ModelBackendError("ARTIFACT_PATH_INVALID")


def _display_name(value: object) -> str:
    text = value if isinstance(value, str) else ""
    cleaned = "".join(char if 32 <= ord(char) < 127 else "?" for char in text)
    return cleaned[:128]


def _reject_reparse_chain(directory: Path, relative: str) -> None:
    current = directory
    for part in relative.split("/"):
        current = current / part
        if is_reparse_point(current):
            raise ModelBackendError("ARTIFACT_PATH_INVALID")


def _discover_bundle(directory: Path, registry: ProviderRegistry) -> CatalogEntryDTO:
    if is_reparse_point(directory):
        raise ModelBackendError("ARTIFACT_PATH_INVALID")
    manifest_path = directory / MANIFEST_FILE_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink() or is_reparse_point(manifest_path):
        raise ModelBackendError("MANIFEST_MISSING")
    try:
        raw = manifest_path.read_bytes()
    except OSError:
        raise ModelBackendError("MANIFEST_MISSING") from None
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ModelBackendError("MANIFEST_INVALID")
    try:
        manifest = ModelManifest.from_dict(strict_json(raw.decode("utf-8")))
    except ModelBackendError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        raise ModelBackendError("MANIFEST_INVALID") from None
    if not manifest.is_catalog_manifest:
        raise ModelBackendError("MANIFEST_INVALID")
    bundle = ModelBundle(manifest, directory)
    _reject_reparse_chain(directory, manifest.artifact)
    bundle.verify()
    checksums = directory / CHECKSUMS_FILE_NAME
    if not checksums.is_file() or checksums.is_symlink() or is_reparse_point(checksums):
        raise ModelBackendError("SHA256SUMS_INVALID")
    registry.resolve_discovery(manifest)
    readiness = manifest.readiness
    assert manifest.model_id is not None and readiness is not None
    return CatalogEntryDTO(
        model_id=manifest.model_id, directory=directory.name, provider=manifest.provider,
        backend=manifest.backend, runtime_contract=str(manifest.runtime_contract),
        classification=str(manifest.classification), experimental=bool(readiness["experimental"]),
        production_ready=bool(readiness["production_ready"]),
        strength_claim=bool(readiness["strength_claim"]),
        artifact_sha256=manifest.artifact_sha256, source=directory)


def scan_catalog(catalog_root: Path | str, *, registry: ProviderRegistry | None = None) -> CatalogScanDTO:
    """Scan direct child bundle directories. Missing root scans as empty.

    Any symlink/junction/reparse point on the root or its existing parent chain
    fails closed before any directory content is read.
    """
    registry = registry if registry is not None else default_registry()
    root = Path(catalog_root)
    _reject_catalog_reparse(root)
    if not root.is_dir():
        return CatalogScanDTO()
    try:
        children = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError:
        raise ModelBackendError("MODEL_UNAVAILABLE") from None
    entries: list[CatalogEntryDTO] = []
    problems: list[CatalogProblemDTO] = []
    for child in children:
        if child.name.startswith("."):
            continue
        try:
            is_directory = child.is_dir()
        except OSError:
            problems.append(CatalogProblemDTO(_display_name(child.name), CATALOG_ROOT_INVALID))
            continue
        if not is_directory:
            continue
        try:
            safe_bundle_directory_name(child.name)
        except ModelBackendError as exc:
            problems.append(CatalogProblemDTO(_display_name(child.name), exc.code))
            continue
        try:
            entries.append(_discover_bundle(child, registry))
        except ModelBackendError as exc:
            problems.append(CatalogProblemDTO(_display_name(child.name), exc.code))
        except Exception:
            problems.append(CatalogProblemDTO(_display_name(child.name), CATALOG_BUNDLE_INVALID))
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.model_id] = counts.get(entry.model_id, 0) + 1
    duplicates = {model_id for model_id, count in counts.items() if count > 1}
    if duplicates:
        for entry in entries:
            if entry.model_id in duplicates:
                problems.append(CatalogProblemDTO(entry.directory, CATALOG_DUPLICATE_MODEL_ID))
        entries = [entry for entry in entries if entry.model_id not in duplicates]
    entries.sort(key=lambda entry: (entry.model_id, entry.directory))
    problems.sort(key=lambda problem: (problem.directory, problem.code))
    return CatalogScanDTO(entries=tuple(entries), problems=tuple(problems))


class ModelCatalog:
    """Filesystem ModelCatalogPort. Catalog and active selection are explicit boundaries.

    The trusted models root (`root`, default: the active document's directory)
    binds both the active transaction and the catalog root; the catalog root must
    live inside it. Every scan/resolve re-validates the catalog root's full
    existing parent chain, so trust is never derived from the checked target itself.
    """

    def __init__(self, catalog_root: Path | str, active_file: Path | str, *,
                 registry: ProviderRegistry | None = None, root: Path | str | None = None) -> None:
        self.catalog_root = Path(catalog_root)
        self.active_file = Path(active_file)
        self.root = Path(root) if root is not None else self.active_file.parent
        root_resolved = self.root.resolve()
        if (not self.active_file.resolve().is_relative_to(root_resolved)
                or not self.catalog_root.resolve().is_relative_to(root_resolved)):
            raise ModelBackendError("ACTIVE_SELECTION_INVALID")
        self._registry = registry if registry is not None else default_registry()

    def scan(self) -> CatalogScanDTO:
        return scan_catalog(self.catalog_root, registry=self._registry)

    def load_active(self) -> ActiveSelectionDTO | None:
        return load_active_selection(self.active_file)

    def save_active(self, selection: ActiveSelectionDTO) -> None:
        save_active_selection(self.active_file, selection, root=self.root)

    def stage_active(self, selection: ActiveSelectionDTO) -> ActiveStateSnapshot:
        return stage_active_selection(self.active_file, selection, root=self.root)

    def commit_active(self) -> None:
        commit_active_selection(self.active_file, root=self.root)

    def restore_active(self, snapshot: ActiveStateSnapshot) -> bool:
        return restore_active_state(snapshot)

    def resolve_bundle(self, model_id: str, directory: str) -> Path:
        """Revalidate one recorded catalog selection against a complete, deduplicated scan.

        The exact `(model_id, directory)` pair must appear exactly once in the
        validated entries. Duplicate model ids, problem directories, tampered or
        replaced targets, and a reparse-tainted root chain all fail closed; no
        directory name, iteration or mtime heuristic is ever used.
        """
        try:
            safe_bundle_directory_name(directory)
        except ModelBackendError:
            raise ModelBackendError("ACTIVE_SELECTION_INVALID") from None
        if not isinstance(model_id, str) or not model_id:
            raise ModelBackendError("ACTIVE_SELECTION_INVALID")
        try:
            scan = self.scan()
        except Exception:
            raise ModelBackendError("ACTIVE_SELECTION_INVALID") from None
        matches = [entry for entry in scan.entries
                   if entry.model_id == model_id and entry.directory == directory]
        if len(matches) != 1:
            raise ModelBackendError("ACTIVE_SELECTION_INVALID")
        return self.catalog_root / directory


__all__ = [
    "CATALOG_BUNDLE_INVALID", "CATALOG_DUPLICATE_MODEL_ID", "CATALOG_ROOT_INVALID",
    "ModelCatalog", "is_reparse_point", "scan_catalog",
]
