"""Independent bounded JSONL protocol codec shared by model client and worker."""
from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any, TextIO

from guance.contracts import RequestIdentity, WORKER_PROTOCOL_VERSION
from guance.ports.model import ModelBackendError

MAX_MESSAGE_BYTES = 2 * 1024 * 1024
MAX_LINE = MAX_MESSAGE_BYTES
IDENTITY_FIELDS = ("request_id", "session_id", "game_id", "state_version", "epoch", "generation")


def _bounded_identity(value: Any, *, allow_empty: bool = True) -> bool:
    return (isinstance(value, str) and len(value) <= 128
            and (allow_empty or bool(value))
            and all(char.isalnum() or char in "._:-" for char in value))


def identity(item: Mapping[str, Any]) -> dict[str, Any]:
    result = {name: item.get(name, "" if name != "state_version" and name != "epoch" and name != "generation" else 0)
              for name in IDENTITY_FIELDS}
    return result


def identity_from_request(request: RequestIdentity) -> dict[str, Any]:
    return request.to_dict()


def envelope(kind: str, ids: Mapping[str, Any], **body: Any) -> dict[str, Any]:
    if not isinstance(kind, str) or not kind or not isinstance(ids, Mapping):
        raise ModelBackendError("PROTOCOL_INVALID")
    result = {"protocol_version": WORKER_PROTOCOL_VERSION, "type": kind, **identity(ids)}
    if set(body) & set(result):
        raise ModelBackendError("PROTOCOL_INVALID")
    result.update(body)
    return result


def dumps(item: Mapping[str, Any]) -> str:
    if not isinstance(item, Mapping):
        raise ModelBackendError("PROTOCOL_INVALID")
    try:
        text = json.dumps(dict(item), ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(",", ":"))
        encoded = text.encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ModelBackendError("PROTOCOL_INVALID") from None
    if len(encoded) + 1 > MAX_MESSAGE_BYTES:
        raise ModelBackendError("PROTOCOL_TOO_LARGE")
    return text + "\n"


def validate_envelope(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ModelBackendError("PROTOCOL_INVALID")
    # Protocol v3: the full request identity (including epoch/generation) is
    # mandatory on every envelope. Legacy four-field envelopes are rejected.
    for name in ("request_id", "session_id", "game_id"):
        if not _bounded_identity(item.get(name), allow_empty=name != "request_id"):
            raise ModelBackendError("PROTOCOL_INVALID")
    for name in ("state_version", "epoch", "generation"):
        value = item.get(name)
        if type(value) is not int or not 0 <= value < 2**63:
            raise ModelBackendError("PROTOCOL_INVALID")
    if not isinstance(item.get("type"), str) or not item["type"]:
        raise ModelBackendError("PROTOCOL_INVALID")
    if type(item.get("protocol_version")) is not int or item["protocol_version"] != WORKER_PROTOCOL_VERSION:
        error = ModelBackendError("PROTOCOL_UNSUPPORTED")
        error.request_identity = identity(item)
        raise error
    return item


def _reject_duplicate_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object member")
        result[key] = value
    return result


def read_message(stream: TextIO) -> dict[str, Any] | None:
    try:
        line = stream.readline(MAX_MESSAGE_BYTES + 1)
        if not line:
            return None
        if len(line.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ModelBackendError("PROTOCOL_TOO_LARGE")
        if not line.endswith("\n"):
            raise ModelBackendError("PROTOCOL_INVALID")
        return validate_envelope(json.loads(
            line, object_pairs_hook=_reject_duplicate_members,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value))))
    except ModelBackendError:
        raise
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise ModelBackendError("PROTOCOL_INVALID") from None


def safe_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    fields = {"status", "provider", "backend", "runtime", "runtime_version", "model_version",
              "runtime_contract", "iteration",
              "model_sha256", "bundle_fingerprint", "deployment_fingerprint", "legacy",
              "score_kind", "classification", "formal_evaluation_status", "champion_status",
              "test_only", "experimental", "production_ready", "strength_claim",
              "public_self_supported", "input_contract", "output_contract",
              "wire_input_contract", "wire_output_contract", "capabilities",
              "requested_device", "actual_device", "device_fallback", "device_reason",
              "torch_cuda_build", "device_evidence"}
    clean: dict[str, Any] = {}
    for key, item in value.items():
        if key not in fields:
            continue
        if type(item) is bool or isinstance(item, str) and len(item) <= 128 and all(c.isalnum() or c in "._:+-" for c in item):
            clean[key] = item
        elif key == "iteration" and type(item) is int and 0 <= item < 2**31:
            clean[key] = item
        elif key == "capabilities" and isinstance(item, Mapping):
            capabilities = {str(name): flag for name, flag in item.items()
                            if isinstance(name, str) and type(flag) is bool and len(name) <= 64}
            if capabilities:
                clean[key] = capabilities
    return clean
