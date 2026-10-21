"""P1-A6/A7/N8: strict active_model.json parser, atomic persistence, fail-closed matrix."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from guance.infrastructure.model.errors import ModelBackendError
from guance.ports.model import ActiveSelectionDTO
from guance.infrastructure.model.selection import (
    commit_active_selection, commit_file_for, load_active_selection, restore_active_state,
    safe_bundle_directory_name, save_active_selection, selected_at_now, stage_active_selection,
    validate_active_selection,
)


def active_file(tmp_path: Path) -> Path:
    return tmp_path / "models" / "active_model.json"


def test_round_trip_catalog_and_legacy(tmp_path):
    target = active_file(tmp_path)
    catalog_selection = ActiveSelectionDTO(kind="catalog", model_id="fabledan-default",
                                           bundle="fabledan-default", selected_at=selected_at_now())
    save_active_selection(target, catalog_selection)
    loaded = load_active_selection(target)
    assert loaded == catalog_selection
    raw = json.loads(target.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1 and raw["kind"] == "catalog"
    legacy_selection = ActiveSelectionDTO(kind="legacy", path=r"D:\models\best.npz",
                                          selected_at=selected_at_now())
    save_active_selection(target, legacy_selection)
    assert load_active_selection(target) == legacy_selection
    assert not list(target.parent.glob(".active-*.tmp"))


def test_missing_document_is_none(tmp_path):
    assert load_active_selection(active_file(tmp_path)) is None


@pytest.mark.parametrize("payload", [
    '{"schema_version":1,"kind":"catalog","model_id":"x","bundle":"x","selected_at":"","extra":1}',
    '{"schema_version":1,"kind":"catalog","model_id":"x","selected_at":""}',
    '{"schema_version":2,"kind":"catalog","model_id":"x","bundle":"x","selected_at":""}',
    '{"schema_version":1,"kind":"other","path":"x","selected_at":""}',
    '{"schema_version":1,"kind":"catalog","model_id":"x","bundle":"x","path":"y","selected_at":""}',
    '{"schema_version":1,"kind":"legacy","model_id":"x","path":"y","selected_at":""}',
    '{"schema_version":1,"kind":"legacy","path":"","selected_at":""}',
    '{"schema_version":1,"kind":"legacy","path":"a\\u0000b","selected_at":""}',
    '{"schema_version":true,"kind":"legacy","path":"x","selected_at":""}',
    '{"schema_version":1,"kind":"legacy","path":"x","selected_at":"'
    + "s" * 65 + '"}',
    '{"schema_version":1,"kind":"catalog","model_id":"bad id","bundle":"x","selected_at":""}',
    '{"schema_version":1,"kind":"catalog","model_id":"x","bundle":"../escape","selected_at":""}',
    '{"schema_version":1,"kind":"catalog","model_id":"x","bundle":"CON","selected_at":""}',
    '{"schema_version":1,"kind":"catalog","model_id":"x","bundle":"a/b","selected_at":""}',
    '{"schema_version":1,"kind":"catalog","model_id":"x","bundle":"x","selected_at":"","selected_at":""}',
    '{"schema_version":NaN,"kind":"legacy","path":"x"}',
    '[]',
    '"catalog"',
])
def test_tampered_documents_fail_closed(tmp_path, payload):
    target = active_file(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload, encoding="utf-8")
    with pytest.raises(ModelBackendError) as exc:
        load_active_selection(target)
    assert exc.value.code == "ACTIVE_SELECTION_INVALID"


def test_oversized_document_fails_closed(tmp_path):
    target = active_file(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"{" + b" " * 5000 + b"}")
    with pytest.raises(ModelBackendError) as exc:
        load_active_selection(target)
    assert exc.value.code == "ACTIVE_SELECTION_INVALID"


def test_unreadable_document_fails_closed(tmp_path, monkeypatch):
    target = active_file(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}", encoding="utf-8")
    original = Path.read_bytes
    def denied(path, *args, **kwargs):
        if path == target:
            raise PermissionError("synthetic")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_bytes", denied)
    with pytest.raises(ModelBackendError) as exc:
        load_active_selection(target)
    assert exc.value.code == "ACTIVE_SELECTION_INVALID"


@pytest.mark.parametrize("selection", [
    ActiveSelectionDTO(kind="catalog", model_id="", bundle="x"),
    ActiveSelectionDTO(kind="catalog", model_id="x", bundle=""),
    ActiveSelectionDTO(kind="catalog", model_id="x", bundle="x", path="y"),
    ActiveSelectionDTO(kind="legacy"),
    ActiveSelectionDTO(kind="legacy", path="x", bundle="x"),
    ActiveSelectionDTO(kind="legacy", path="x", model_id="y"),
    ActiveSelectionDTO(kind="", path="x"),
])
def test_invalid_save_payload_rejected(selection):
    with pytest.raises(ModelBackendError) as exc:
        validate_active_selection(selection)
    assert exc.value.code == "ACTIVE_SELECTION_INVALID"


def test_save_failure_propagates(tmp_path, monkeypatch):
    import guance.infrastructure.model.selection as selection_module
    class Broken:
        def __init__(self, *args, **kwargs):
            raise OSError(28, "synthetic disk full")
    monkeypatch.setattr(selection_module.tempfile, "NamedTemporaryFile", Broken)
    with pytest.raises(OSError):
        save_active_selection(active_file(tmp_path),
                              ActiveSelectionDTO(kind="legacy", path="x"))
    assert not active_file(tmp_path).exists()


def test_safe_bundle_directory_name_accepts_normal_names():
    for name in ("bundle", "bundle-1", "bundle.v2", "模型包"):
        assert safe_bundle_directory_name(name) == name


# --- P0 repair: commit marker protocol ----------------------------------------


CATALOG_SELECTION = ActiveSelectionDTO(kind="catalog", model_id="fabledan-default",
                                       bundle="fabledan-default", selected_at="2026-10-16T00:00:00+00:00")
LEGACY_SELECTION = ActiveSelectionDTO(kind="legacy", path=r"D:\models\best.npz",
                                      selected_at="2026-10-16T00:00:00+00:00")


def test_uncommitted_document_without_marker_fails_closed(tmp_path):
    target = active_file(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    save_active_selection(target, CATALOG_SELECTION)
    marker = commit_file_for(target)
    marker.unlink()
    with pytest.raises(ModelBackendError) as exc:
        load_active_selection(target)
    assert exc.value.code == "ACTIVE_SELECTION_INVALID"


def test_orphan_marker_without_document_fails_closed(tmp_path):
    target = active_file(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    save_active_selection(target, CATALOG_SELECTION)
    target.unlink()
    with pytest.raises(ModelBackendError) as exc:
        load_active_selection(target)
    assert exc.value.code == "ACTIVE_SELECTION_INVALID"


@pytest.mark.parametrize("marker_text", [
    "not-json",
    '{"schema_version":1}',
    '{"schema_version":1,"active_sha256":"' + "0" * 64 + '"}',
    '{"schema_version":2,"active_sha256":"' + "a" * 64 + '"}',
    '{"schema_version":1,"active_sha256":"short"}',
    '{"schema_version":1,"active_sha256":"' + "a" * 64 + '","extra":1}',
    '{"schema_version":1,"active_sha256":"' + "a" * 64 + '","active_sha256":"' + "b" * 64 + '"}',
])
def test_tampered_marker_fails_closed(tmp_path, marker_text):
    target = active_file(tmp_path)
    save_active_selection(target, CATALOG_SELECTION)
    commit_file_for(target).write_text(marker_text, encoding="utf-8")
    with pytest.raises(ModelBackendError) as exc:
        load_active_selection(target)
    assert exc.value.code == "ACTIVE_SELECTION_INVALID"


def test_stage_is_not_committed_until_commit_marker(tmp_path):
    target = active_file(tmp_path)
    save_active_selection(target, CATALOG_SELECTION)
    snapshot = stage_active_selection(target, LEGACY_SELECTION)
    with pytest.raises(ModelBackendError) as exc:
        load_active_selection(target)
    assert exc.value.code == "ACTIVE_SELECTION_INVALID"
    commit_active_selection(target)
    assert load_active_selection(target) == LEGACY_SELECTION
    assert commit_file_for(target).exists()
    assert restore_active_state(snapshot) is True
    assert load_active_selection(target) == CATALOG_SELECTION


def test_restore_recreates_exact_bytes_and_absence(tmp_path):
    target = active_file(tmp_path)
    snapshot_empty = stage_active_selection(target, CATALOG_SELECTION)
    save_active_selection(target, LEGACY_SELECTION)
    before_doc = target.read_bytes()
    before_marker = commit_file_for(target).read_bytes()
    snapshot_committed = stage_active_selection(target, CATALOG_SELECTION)
    assert restore_active_state(snapshot_committed) is True
    assert target.read_bytes() == before_doc
    assert commit_file_for(target).read_bytes() == before_marker
    assert load_active_selection(target) == LEGACY_SELECTION
    assert restore_active_state(snapshot_empty) is True
    assert not target.exists() and not commit_file_for(target).exists()
    assert load_active_selection(target) is None


def test_restore_failure_returns_false_and_stays_fail_closed(tmp_path, monkeypatch):
    import guance.infrastructure.model.selection as selection_module
    target = active_file(tmp_path)
    save_active_selection(target, CATALOG_SELECTION)
    snapshot = stage_active_selection(target, LEGACY_SELECTION)
    original = selection_module._write_bytes_atomic
    def failing(path, data, *, root):
        if Path(path) == target:
            raise OSError(28, "synthetic disk full")
        return original(path, data, root=root)
    monkeypatch.setattr(selection_module, "_write_bytes_atomic", failing)
    assert restore_active_state(snapshot) is False
    monkeypatch.setattr(selection_module, "_write_bytes_atomic", original)
    # Staged document + old marker hash mismatch: restart stays fail-closed.
    with pytest.raises(ModelBackendError) as exc:
        load_active_selection(target)
    assert exc.value.code == "ACTIVE_SELECTION_INVALID"


def test_active_transaction_root_containment(tmp_path):
    outside = tmp_path / "outside" / "active_model.json"
    with pytest.raises(ModelBackendError) as exc:
        stage_active_selection(outside, CATALOG_SELECTION, root=tmp_path / "models")
    assert exc.value.code == "ACTIVE_SELECTION_INVALID"
    assert not outside.exists()
    inside = tmp_path / "models" / "active_model.json"
    snapshot = stage_active_selection(inside, CATALOG_SELECTION, root=tmp_path / "models")
    assert snapshot.target == inside
    assert commit_active_selection(inside, root=tmp_path / "models") is None
    assert load_active_selection(inside) == CATALOG_SELECTION


# --- P0 repair: real Windows reparse boundaries -------------------------------


def test_real_junction_models_parent_chain_fails_closed(tmp_path):
    from tests.model.windows_junction import make_junction
    real_models = tmp_path / "models-real"
    real_models.mkdir()
    models = tmp_path / "models"
    make_junction(models, real_models)
    target = models / "active_model.json"
    with pytest.raises(ModelBackendError) as exc:
        stage_active_selection(target, CATALOG_SELECTION)
    assert exc.value.code == "ACTIVE_SELECTION_INVALID"
    with pytest.raises(ModelBackendError) as exc:
        load_active_selection(target)
    assert exc.value.code == "ACTIVE_SELECTION_INVALID"
    assert not (real_models / "active_model.json").exists()


def test_real_junction_active_document_fails_closed(tmp_path):
    from tests.model.windows_junction import make_junction
    target = active_file(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    junk = tmp_path / "junk"
    junk.mkdir()
    make_junction(target, junk)
    with pytest.raises(ModelBackendError) as exc:
        load_active_selection(target)
    assert exc.value.code == "ACTIVE_SELECTION_INVALID"
    with pytest.raises(ModelBackendError) as exc:
        stage_active_selection(target, CATALOG_SELECTION)
    assert exc.value.code == "ACTIVE_SELECTION_INVALID"


def test_real_junction_active_marker_fails_closed(tmp_path):
    from tests.model.windows_junction import make_junction
    target = active_file(tmp_path)
    save_active_selection(target, CATALOG_SELECTION)
    marker = commit_file_for(target)
    marker.unlink()
    junk = tmp_path / "marker-junk"
    junk.mkdir()
    make_junction(marker, junk)
    with pytest.raises(ModelBackendError) as exc:
        load_active_selection(target)
    assert exc.value.code == "ACTIVE_SELECTION_INVALID"


def test_real_file_symlink_active_document_fails_closed(tmp_path):
    from tests.model.windows_junction import make_file_symlink
    target = active_file(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    other = tmp_path / "other.json"
    other.write_text("{}", encoding="utf-8")
    make_file_symlink(target, other)
    with pytest.raises(ModelBackendError) as exc:
        load_active_selection(target)
    assert exc.value.code == "ACTIVE_SELECTION_INVALID"
    with pytest.raises(ModelBackendError) as exc:
        stage_active_selection(target, CATALOG_SELECTION)
    assert exc.value.code == "ACTIVE_SELECTION_INVALID"
