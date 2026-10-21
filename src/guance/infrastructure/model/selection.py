"""Strict active-selection document, commit marker and shared path safety.

Layout (under the models root):

    active_model.json          selection document: the single model-choice authority
    active_model.commit.json   commit marker: sha256 of the exact document bytes

Commit protocol (used by the controller and by legacy migration):

    capture  -> validate layout, snapshot the exact prior document/marker bytes
    stage    -> atomically write the new document (not yet committed)
    commit   -> atomically write the marker for the current document bytes

Startup accepts a document only when its bytes hash matches the marker. A staged
document without a matching marker (crash, dry-run, failed rollback) fails
closed, so a failed selection can never be activated by a restart even when a
later rollback attempt also fails. Any reparse point on the document, marker or
parent chain, and any path outside the declared models root, is rejected.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat as stat_module
import tempfile

from guance.ports.model import ActiveSelectionDTO
from .errors import ModelBackendError

ACTIVE_SCHEMA_VERSION = 1
ACTIVE_FILE_MAX_BYTES = 4096
COMMIT_FILE_MAX_BYTES = 512
_KINDS = ("catalog", "legacy")
_COMMON_KEYS = frozenset({"schema_version", "kind", "selected_at"})
_KIND_KEYS = {"catalog": frozenset({"model_id", "bundle"}), "legacy": frozenset({"path"})}
_MARKER_KEYS = frozenset({"schema_version", "active_sha256"})
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def is_reparse_point(path: Path | str) -> bool:
    """Symlink/junction/reparse detection shared by catalog and active-selection checks."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return bool(stat_module.S_ISLNK(info.st_mode)
                or int(getattr(info, "st_file_attributes", 0)) & _FILE_ATTRIBUTE_REPARSE_POINT)


def reject_reparse_chain(path: Path | str) -> None:
    """Reject a symlink/reparse point anywhere on the path or its existing parents."""
    for candidate in reversed([Path(path), *Path(path).parents]):
        if not candidate.exists():
            continue
        if candidate.is_symlink() or is_reparse_point(candidate):
            raise ModelBackendError("ACTIVE_SELECTION_INVALID")


def safe_bundle_directory_name(value: object) -> str:
    """One bounded, non-escaping directory segment name (no ADS/reserved/traversal)."""
    if (not isinstance(value, str) or not value or len(value) > 128
            or value in (".", "..") or value.startswith(".")
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or any(char in value for char in '<>:"|?*\\/')
            or value.endswith((".", " "))
            or PureWindowsPath(value).is_reserved()):
        raise ModelBackendError("ARTIFACT_PATH_INVALID")
    return value


def _bounded_text(value: object, *, maximum: int, allow_empty: bool = False) -> str:
    if (not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value)
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ModelBackendError("ACTIVE_SELECTION_INVALID")
    return value


def validate_active_selection(selection: object) -> ActiveSelectionDTO:
    if type(selection) is not ActiveSelectionDTO:
        raise ModelBackendError("ACTIVE_SELECTION_INVALID")
    if selection.kind not in _KINDS:
        raise ModelBackendError("ACTIVE_SELECTION_INVALID")
    _bounded_text(selection.selected_at, maximum=64, allow_empty=True)
    if selection.kind == "catalog":
        if (not isinstance(selection.model_id, str) or not _IDENTIFIER.fullmatch(selection.model_id)
                or selection.path != ""):
            raise ModelBackendError("ACTIVE_SELECTION_INVALID")
        try:
            bundle = safe_bundle_directory_name(selection.bundle)
        except ModelBackendError:
            raise ModelBackendError("ACTIVE_SELECTION_INVALID") from None
        return ActiveSelectionDTO(kind="catalog", model_id=selection.model_id,
                                  bundle=bundle, selected_at=selection.selected_at)
    if selection.model_id != "" or selection.bundle != "":
        raise ModelBackendError("ACTIVE_SELECTION_INVALID")
    path = _bounded_text(selection.path, maximum=4096)
    return ActiveSelectionDTO(kind="legacy", path=path, selected_at=selection.selected_at)


def to_wire(selection: ActiveSelectionDTO) -> dict[str, object]:
    checked = validate_active_selection(selection)
    payload: dict[str, object] = {"schema_version": ACTIVE_SCHEMA_VERSION, "kind": checked.kind,
                                  "selected_at": checked.selected_at}
    if checked.kind == "catalog":
        payload["model_id"] = checked.model_id
        payload["bundle"] = checked.bundle
    else:
        payload["path"] = checked.path
    return payload


def from_wire(data: object) -> ActiveSelectionDTO:
    if not isinstance(data, dict):
        raise ModelBackendError("ACTIVE_SELECTION_INVALID")
    kind = data.get("kind")
    if kind not in _KINDS:
        raise ModelBackendError("ACTIVE_SELECTION_INVALID")
    if set(data) != _COMMON_KEYS | _KIND_KEYS[kind]:
        raise ModelBackendError("ACTIVE_SELECTION_INVALID")
    if type(data.get("schema_version")) is not int or data["schema_version"] != ACTIVE_SCHEMA_VERSION:
        raise ModelBackendError("ACTIVE_SELECTION_INVALID")
    selected_at = _bounded_text(data.get("selected_at"), maximum=64, allow_empty=True)
    if kind == "catalog":
        model_id = data.get("model_id")
        if not isinstance(model_id, str) or not _IDENTIFIER.fullmatch(model_id):
            raise ModelBackendError("ACTIVE_SELECTION_INVALID")
        try:
            bundle = safe_bundle_directory_name(data.get("bundle"))
        except ModelBackendError:
            raise ModelBackendError("ACTIVE_SELECTION_INVALID") from None
        return ActiveSelectionDTO(kind="catalog", model_id=model_id, bundle=bundle,
                                  selected_at=selected_at)
    path = _bounded_text(data.get("path"), maximum=4096)
    return ActiveSelectionDTO(kind="legacy", path=path, selected_at=selected_at)


def commit_file_for(path: Path | str) -> Path:
    target = Path(path)
    return target.with_name(target.stem + ".commit.json")


@dataclass(frozen=True, slots=True)
class ActiveStateSnapshot:
    """Exact prior bytes for both files; None means the file did not exist."""

    root: Path
    target: Path
    marker: Path
    document: bytes | None
    marker_bytes: bytes | None


def _read_bounded(path: Path, maximum: int) -> bytes | None:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise ModelBackendError("ACTIVE_SELECTION_INVALID") from None
    if len(raw) > maximum:
        raise ModelBackendError("ACTIVE_SELECTION_INVALID")
    return raw


def _check_layout(target: Path, *, root: Path) -> None:
    root_resolved = root.resolve()
    if not target.parent.resolve().is_relative_to(root_resolved):
        raise ModelBackendError("ACTIVE_SELECTION_INVALID")
    reject_reparse_chain(target)
    marker = commit_file_for(target)
    reject_reparse_chain(marker)
    for path in (target, marker):
        if path.exists() and (path.is_symlink() or is_reparse_point(path)):
            raise ModelBackendError("ACTIVE_SELECTION_INVALID")


def _write_bytes_atomic(path: Path, data: bytes, *, root: Path) -> None:
    _check_layout(path, root=root)
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    owned: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=directory, prefix=".active-",
                                         suffix=".tmp", delete=False) as stream:
            owned = Path(stream.name)
            stream.write(data)
            stream.flush()
        owned.replace(path)
    finally:
        if owned is not None:
            try:
                owned.unlink(missing_ok=True)
            except OSError:
                pass


def capture_active_state(path: Path | str, *, root: Path | str | None = None) -> ActiveStateSnapshot:
    """Validate the active layout and snapshot the exact prior bytes of both files."""
    target = Path(path)
    root_path = Path(root) if root is not None else target.parent
    _check_layout(target, root=root_path)
    marker = commit_file_for(target)
    document = _read_bounded(target, ACTIVE_FILE_MAX_BYTES)
    marker_bytes = _read_bounded(marker, COMMIT_FILE_MAX_BYTES)
    return ActiveStateSnapshot(root=root_path, target=target, marker=marker,
                               document=document, marker_bytes=marker_bytes)


def stage_active_selection(path: Path | str, selection: ActiveSelectionDTO, *,
                           root: Path | str | None = None) -> ActiveStateSnapshot:
    """Atomically write the new document; the selection is not committed yet."""
    payload = to_wire(selection)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    snapshot = capture_active_state(path, root=root)
    _write_bytes_atomic(snapshot.target, encoded, root=snapshot.root)
    return snapshot


def commit_active_selection(path: Path | str, *, root: Path | str | None = None) -> None:
    """Commit point: write the marker matching the current document bytes."""
    target = Path(path)
    root_path = Path(root) if root is not None else target.parent
    _check_layout(target, root=root_path)
    document = _read_bounded(target, ACTIVE_FILE_MAX_BYTES)
    if document is None:
        raise ModelBackendError("ACTIVE_SELECTION_INVALID")
    marker_payload = {"schema_version": ACTIVE_SCHEMA_VERSION,
                      "active_sha256": hashlib.sha256(document).hexdigest()}
    encoded = json.dumps(marker_payload, ensure_ascii=False, indent=2).encode("utf-8")
    _write_bytes_atomic(commit_file_for(target), encoded, root=root_path)


def restore_active_state(snapshot: ActiveStateSnapshot) -> bool:
    """Best-effort exact restore of the snapshotted document and marker bytes.

    Returns False when any restore step failed. The marker protocol keeps such a
    state fail-closed at startup regardless, because the on-disk document can no
    longer match a marker that was never (or no longer) committed for it.
    """
    try:
        for path, payload, maximum in ((snapshot.target, snapshot.document, ACTIVE_FILE_MAX_BYTES),
                                       (snapshot.marker, snapshot.marker_bytes, COMMIT_FILE_MAX_BYTES)):
            if payload is None:
                path.unlink(missing_ok=True)
            else:
                if len(payload) > maximum:
                    return False
                _write_bytes_atomic(path, payload, root=snapshot.root)
        return True
    except Exception:
        return False


def save_active_selection(path: Path | str, selection: ActiveSelectionDTO, *,
                          root: Path | str | None = None) -> None:
    """Stage + commit convenience with rollback when the commit step fails."""
    snapshot = stage_active_selection(path, selection, root=root)
    try:
        commit_active_selection(path, root=root)
    except Exception:
        restore_active_state(snapshot)
        raise


def load_active_selection(path: Path | str) -> ActiveSelectionDTO | None:
    """None when no selection document exists; uncommitted/invalid documents fail closed."""
    target = Path(path)
    reject_reparse_chain(target)
    marker = commit_file_for(target)
    for candidate in (target, marker):
        if candidate.exists() and (candidate.is_symlink() or is_reparse_point(candidate)):
            raise ModelBackendError("ACTIVE_SELECTION_INVALID")
    document = _read_bounded(target, ACTIVE_FILE_MAX_BYTES)
    marker_bytes = _read_bounded(marker, COMMIT_FILE_MAX_BYTES)
    if document is None:
        if marker_bytes is None:
            return None
        raise ModelBackendError("ACTIVE_SELECTION_INVALID")
    if marker_bytes is None:
        raise ModelBackendError("ACTIVE_SELECTION_INVALID")
    try:
        marker_data = json.loads(marker_bytes.decode("utf-8"), object_pairs_hook=_reject_duplicate_members,
                                 parse_constant=_reject_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise ModelBackendError("ACTIVE_SELECTION_INVALID") from None
    if (not isinstance(marker_data, dict) or set(marker_data) != _MARKER_KEYS
            or type(marker_data.get("schema_version")) is not int
            or marker_data["schema_version"] != ACTIVE_SCHEMA_VERSION
            or not isinstance(marker_data.get("active_sha256"), str)
            or not _SHA256.fullmatch(marker_data["active_sha256"])
            or marker_data["active_sha256"] != hashlib.sha256(document).hexdigest()):
        raise ModelBackendError("ACTIVE_SELECTION_INVALID")
    try:
        data = json.loads(document.decode("utf-8"), object_pairs_hook=_reject_duplicate_members,
                          parse_constant=_reject_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise ModelBackendError("ACTIVE_SELECTION_INVALID") from None
    return from_wire(data)


def selected_at_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _reject_duplicate_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object member")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value}")


__all__ = [
    "ACTIVE_FILE_MAX_BYTES", "ACTIVE_SCHEMA_VERSION", "ActiveStateSnapshot",
    "capture_active_state", "commit_active_selection", "commit_file_for", "from_wire",
    "is_reparse_point", "load_active_selection", "reject_reparse_chain", "restore_active_state",
    "safe_bundle_directory_name", "save_active_selection", "selected_at_now",
    "stage_active_selection", "to_wire", "validate_active_selection",
]
