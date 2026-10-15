"""GMV2-P0-001/002/005: pinned artifact and restricted-loader negatives.

The optional real checkpoint is read in place, never copied. Temporary artifacts
below contain only corrupt/synthetic bytes. Fixture reprs never expose payloads.
"""
from copy import deepcopy
from dataclasses import asdict, replace
import ast
import hashlib
import io
import json
import os
from pathlib import Path

import pytest

from guance.infrastructure.model.errors import ModelBackendError
from guance.infrastructure.model.manifest import ModelBundle, ModelManifest, load_bundle
from guance.infrastructure.model.marvel_v2 import checkpoint
from guance.infrastructure.model.marvel_v2.identity import (
    CHECKPOINT_SHA256, CONFIG_SHA256, DESCRIPTOR_FIELDS, CheckpointIdentity, fixed_manifest,
)
from guance.infrastructure.model.marvel_v2.state_contract import STATE_SHAPES


@pytest.fixture(scope="module")
def torch_module():
    torch = pytest.importorskip("torch", reason="optional Torch runtime not installed")
    threads = torch.get_num_threads()
    torch.set_num_threads(1)
    yield torch
    torch.set_num_threads(threads)


@pytest.fixture(scope="module")
def checkpoint_bundle():
    path = Path(os.environ.get("GUANCE_MARVEL_V2_CHECKPOINT", (
        r"D:\project\guandan\runs\M09_full_training\remote\ippo_pilot_failed_20261011"
        r"\extracted-output\evaluation_checkpoints\iter000100.pt")))
    if not path.is_file():
        pytest.skip("fixed evaluation checkpoint not available; no download or copy")
    return ModelBundle(fixed_manifest(path.name), path.parent)


class _RedactedPayload:
    def __init__(self, data):
        self.data = data

    def __repr__(self):
        return "<restricted checkpoint fixture; contents redacted>"


@pytest.fixture(scope="module")
def checkpoint_payload(checkpoint_bundle, torch_module):
    raw = checkpoint.verified_artifact_bytes(checkpoint_bundle)
    return _RedactedPayload(checkpoint._restricted_load(raw))


def assert_rejected(payload, torch):
    with pytest.raises(ModelBackendError) as exc:
        checkpoint._parse_payload(payload, torch)
    assert exc.value.code == "MODEL_LOAD_FAILED"
    assert "project" not in str(exc.value).lower()


def test_fixed_explicit_bundle_restricted_load(checkpoint_bundle, torch_module, monkeypatch):
    calls = []
    original = torch_module.load
    def load(stream, **kwargs):
        calls.append(kwargs)
        assert hashlib.sha256(stream.getvalue()).hexdigest() == CHECKPOINT_SHA256
        return original(stream, **kwargs)
    monkeypatch.setattr(torch_module, "load", load)
    bundle = load_bundle(checkpoint_bundle)
    loaded = checkpoint.load_checkpoint(bundle)
    assert calls == [{"map_location": "cpu", "weights_only": True}]
    assert len(loaded.state_dict) == 78
    assert loaded.state_dict.keys() == STATE_SHAPES.keys()
    assert checkpoint.canonical_json_hash(loaded.resolved_config) == CONFIG_SHA256
    assert loaded.identity == CheckpointIdentity()
    assert all(tuple(t.shape) == STATE_SHAPES[k] and t.dtype == torch_module.float32
               and bool(torch_module.isfinite(t).all()) for k, t in loaded.state_dict.items())
    rendered = repr(loaded)
    assert "agent_state_dict" not in rendered and "logging_dir" not in rendered
    metadata = json.dumps(asdict(loaded.identity))
    assert len(metadata) < 2500
    assert not any(term in metadata for term in ("D:", "logging_dir", "deal_hashes", "state_dict"))
    with pytest.raises((AttributeError, TypeError)):
        loaded.identity.production_ready = True


@pytest.mark.parametrize("field", sorted(checkpoint._PAYLOAD_KEYS))
def test_missing_checkpoint_fields_fail_closed(checkpoint_payload, torch_module, field):
    data = dict(checkpoint_payload.data)
    del data[field]
    assert_rejected(data, torch_module)


@pytest.mark.parametrize("field,value", [
    ("schema", "unknown"), ("algorithm", "vrpo"), ("architecture", "v1.2"),
    ("iteration", 101), ("iteration", 100.0), ("iteration", True),
    ("resume_classification", "training"), ("training_resume_allowed", True),
    ("training_resume_allowed", 0), ("protocol_version", "GD-ACTION-0.1"),
    ("backend", "python"), ("source_hash", "0" * 64),
    ("source_parent_commit", "0" * 40), ("resolved_config_hash", "0" * 64),
    ("classification", "training"), ("unknown", {}),
])
def test_wrong_scalar_identity_rejected(checkpoint_payload, torch_module, field, value):
    data = {**checkpoint_payload.data, field: value}
    assert_rejected(data, torch_module)


@pytest.mark.parametrize("kind", ["config", "source", "backend", "source_rehash", "backend_rehash",
                                   "config_rehash", "merged", "distributed_extra", "provenance_extra"])
def test_nested_identity_cannot_self_certify(checkpoint_payload, torch_module, kind):
    data = {**checkpoint_payload.data}
    if kind.startswith("config"):
        data["resolved_config"] = deepcopy(data["resolved_config"])
        data["resolved_config"]["model"]["feature_dim"] = 128
        if kind.endswith("rehash"):
            data["resolved_config_hash"] = checkpoint.canonical_json_hash(data["resolved_config"])
    elif kind.startswith(("source", "backend")):
        key = "source_fingerprint" if kind.startswith("source") else "backend_identity"
        data[key] = deepcopy(data[key])
        data[key]["schema"] = "changed"
        if kind.endswith("rehash"):
            data[key]["hash"] = checkpoint.canonical_json_hash({k: v for k, v in data[key].items() if k != "hash"})
    elif kind == "merged":
        data["merged_policy"] = {**data["merged_policy"], "beta": 0.5}
    else:
        key = "distributed_training" if kind == "distributed_extra" else "training_data_provenance"
        data[key] = {**data[key], "unknown": True}
    assert_rejected(data, torch_module)


@pytest.mark.parametrize("name", list(STATE_SHAPES))
def test_each_of_78_exact_shapes_is_enforced(checkpoint_payload, torch_module, name):
    state = dict(checkpoint_payload.data["agent_state_dict"])
    state[name] = torch_module.zeros((*STATE_SHAPES[name], 1))
    assert_rejected({**checkpoint_payload.data, "agent_state_dict": state}, torch_module)


@pytest.mark.parametrize("kind", ["missing", "extra", "float64", "int64", "bool", "nan", "inf",
                                   "minus_inf", "not_tensor", "meta", "sparse"])
def test_state_keys_dtype_layout_device_and_finiteness(checkpoint_payload, torch_module, kind):
    state = dict(checkpoint_payload.data["agent_state_dict"])
    name = "state_tokens_embed.weight"
    tensor = state[name]
    if kind == "missing":
        del state[name]
    elif kind == "extra":
        state["unexpected.weight"] = tensor
    elif kind in ("float64", "int64", "bool"):
        state[name] = tensor.to(getattr(torch_module, kind))
    elif kind in ("nan", "inf", "minus_inf"):
        state[name] = tensor.clone()
        state[name].view(-1)[0] = float("-inf" if kind == "minus_inf" else kind)
    elif kind == "not_tensor":
        state[name] = 1
    elif kind == "meta":
        state[name] = tensor.to("meta")
    else:
        state[name] = tensor.to_sparse()
    assert_rejected({**checkpoint_payload.data, "agent_state_dict": state}, torch_module)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), (1, 2), {1: "value"}, object()])
def test_canonical_hash_rejects_non_json(value):
    with pytest.raises(ModelBackendError) as exc:
        checkpoint.canonical_json_hash({"x": value})
    assert exc.value.code == "MODEL_LOAD_FAILED"


def test_canonical_hash_is_utf8_sorted_compact_and_finite():
    data = {"b": "中文", "a": [1, True, None]}
    expected = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"),
                                        ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    assert checkpoint.canonical_json_hash(data) == expected


@pytest.mark.parametrize("missing", [False, True])
def test_bad_artifact_rejected_before_torch_import(tmp_path, monkeypatch, missing):
    if not missing:
        (tmp_path / "bad.pt").write_bytes(b"synthetic corrupt artifact")
    def forbidden():
        pytest.fail("Torch must not be imported before file integrity passes")
    monkeypatch.setattr(checkpoint, "_torch", forbidden)
    with pytest.raises(ModelBackendError) as exc:
        checkpoint.load_checkpoint(ModelBundle(fixed_manifest("bad.pt"), tmp_path))
    assert exc.value.code == ("ARTIFACT_MISSING" if missing else "ARTIFACT_HASH_MISMATCH")


def test_snapshot_rehashed_after_bundle_verification(checkpoint_bundle, monkeypatch):
    original = Path.open
    count = 0
    def open_file(path, *args, **kwargs):
        nonlocal count
        if path == checkpoint_bundle.artifact_path:
            count += 1
            if count == 2:
                return io.BytesIO(b"synthetic replacement after verification")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", open_file)
    monkeypatch.setattr(checkpoint, "_torch", lambda: pytest.fail("replacement reached Torch"))
    with pytest.raises(ModelBackendError) as exc:
        checkpoint.load_checkpoint(checkpoint_bundle)
    assert exc.value.code == "ARTIFACT_HASH_MISMATCH"


def test_bare_pt_and_legacy_descriptor_remain_rejected(tmp_path):
    with pytest.raises(ModelBackendError) as exc:
        load_bundle(tmp_path / "model.pt")
    assert exc.value.code == "MODEL_SOURCE_INVALID"
    for source in (tmp_path / "model.pt", ModelBundle(fixed_manifest("model.pt"), tmp_path, legacy=True)):
        with pytest.raises(ModelBackendError) as exc:
            checkpoint.load_checkpoint(source)
        assert exc.value.code == "MODEL_SOURCE_INVALID"


@pytest.mark.parametrize("field", list(DESCRIPTOR_FIELDS))
def test_every_descriptor_identity_field_is_pinned(field):
    manifest = replace(fixed_manifest("model.pt"), **{field: "0" * 64 if field == "artifact_sha256" else "wrong"})
    from guance.infrastructure.model.registry import default_registry
    with pytest.raises(ModelBackendError):
        default_registry().resolve(manifest)


@pytest.mark.parametrize("artifact", ["../model.pt", "C:/model.pt", "/model.pt", "sub/../../model.pt", "x:stream"])
def test_v2_bundle_paths_stay_strict(artifact):
    with pytest.raises(ModelBackendError) as exc:
        fixed_manifest(artifact)
    assert exc.value.code == "ARTIFACT_PATH_INVALID"


def test_v2_does_not_add_an_unbounded_manifest_metadata_blob():
    data = fixed_manifest("model.pt").to_dict()
    data["metadata"] = {"arbitrary": "value"}
    with pytest.raises(ModelBackendError) as exc:
        ModelManifest.from_dict(data)
    assert exc.value.code == "MANIFEST_INVALID"


class _MaliciousPayload:
    def __init__(self, sentinel):
        self.sentinel = sentinel

    def __reduce__(self):
        return eval, ("open(" + repr(str(self.sentinel)) + ", 'w').write('executed')",)


def test_restricted_primitive_rejects_custom_objects_without_execution(tmp_path, torch_module):
    # Exercise the private primitive independently: the public hash gate would
    # reject these bytes even before this defense-in-depth deserializer test.
    sentinel = tmp_path / "must-not-exist"
    stream = io.BytesIO()
    torch_module.save({"custom": _MaliciousPayload(sentinel)}, stream)
    with pytest.raises(ModelBackendError) as exc:
        checkpoint._restricted_load(stream.getvalue())
    assert exc.value.code == "MODEL_LOAD_FAILED"
    assert not sentinel.exists()
    with pytest.raises(ModelBackendError) as exc:
        checkpoint._restricted_load(b"corrupt synthetic bytes")
    assert exc.value.code == "MODEL_LOAD_FAILED"


def test_missing_torch_is_stable_unavailable(monkeypatch):
    def missing(name):
        raise ImportError("private dependency location must not be exposed")
    monkeypatch.setattr(checkpoint.importlib, "import_module", missing)
    with pytest.raises(ModelBackendError) as exc:
        checkpoint._restricted_load(b"synthetic")
    assert exc.value.code == "MODEL_UNAVAILABLE"
    assert "private" not in str(exc.value)


def test_only_production_torch_load_is_restricted():
    directory = Path(checkpoint.__file__).parent
    loads = []
    for source in directory.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        assert "add_safe_globals" not in text and "safe_globals(" not in text
        assert "sys.path" not in text and "D:\\project" not in text
        for node in ast.walk(ast.parse(text)):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "torch"
                    and node.func.attr == "load"):
                values = {kw.arg: ast.literal_eval(kw.value) for kw in node.keywords}
                loads.append(values)
    assert loads == [{"map_location": "cpu", "weights_only": True}]
