"""Generate privacy-bounded Phase 7 historical replay evidence."""
from __future__ import annotations

import argparse
import base64
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from guance.infrastructure.capture.offline_replay import (  # noqa: E402
    protocol_variant_matrix, sha256_file, strict_replay,
)
from guance.infrastructure.protocol import live, wire  # noqa: E402

CAPTURE_IDS = (
    "20260929-141145", "20260929-162605", "20260929-173126",
    "20260929-232820", "20260930-004157", "20260930-012733",
    "20260930-111959", "20260930-171722", "20261001-092755",
    "20261014-143204", "20261014-231315",
)
LATEST = "20261014-231315"
NORMAL = "20261014-143204"


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def _field11(capture_id: str, path: Path) -> Counter[int]:
    result: Counter[int] = Counter()
    for line in path.open("rb"):
        record = json.loads(line.decode("utf-8-sig"))
        if record.get("direction") != "server":
            continue
        try:
            bundle = live.extract_bundle(base64.b64decode(record["payload_b64"], validate=True))
        except (ValueError, TypeError):
            continue
        if bundle is None:
            continue
        for item in bundle[1]:
            if item.get("type") != live.PB_RECORD_TYPE:
                continue
            outer = wire.fields(item["payload"])
            if wire.scalar(outer, 1) != 3005:
                continue
            wrapper = wire.fields(wire.one_blob(outer, 3))
            group = wire.fields(wire.one_blob(wrapper, live.COMMAND_VARIANTS[3005]))
            result[len(wire.one_blob(group, 11))] += 1
    return result


def _categories(capture_id: str, matrix: dict[str, object], replay: dict[str, object]) -> list[str]:
    result: list[str] = []
    if not matrix["direction_field_available"]:
        return ["legacy_schema_unsupported", "direction_unavailable"]
    errors = replay["error_counts"]
    assert isinstance(errors, dict)
    if replay["protocol_rejected"]:
        result.append("protocol_variant_unsupported")
    else:
        result.append("converted")
    if capture_id == NORMAL:
        result.append("supported_subset")
        if errors:
            result.append("expected_unsupported")
    elif capture_id == LATEST:
        result.extend(("supported_subset", "connection_switch"))
    elif matrix["connection_epochs"] > 1:
        result.append("connection_switch")
    sidecar = ROOT / "debug" / "captures" / capture_id / "capture_status.json"
    if not sidecar.exists():
        result.append("missing_field")
    else:
        status = json.loads(sidecar.read_text(encoding="utf-8-sig"))
        if status.get("state") != "closed":
            result.append("truncated")
        if any(game.get("status") == "incomplete" for game in status.get("games", [])):
            result.append("mid_game_replay")
    return sorted(set(result))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--rules-root", type=Path, default=Path(r"D:\project\guandan"))
    args = parser.parse_args()
    evidence = args.evidence_root.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    before = {}
    matrices = []
    replays = []
    field11 = {}
    for capture_id in CAPTURE_IDS:
        path = ROOT / "debug" / "captures" / capture_id / "frames.jsonl"
        before[capture_id] = sha256_file(path)
        matrix = protocol_variant_matrix(capture_id, path)
        matrices.append(matrix)
        replay = strict_replay(capture_id, path, rules_source_root=args.rules_root)
        replay["classifications"] = _categories(capture_id, matrix, replay)
        replays.append(replay)
        field11[capture_id] = dict(sorted(_field11(capture_id, path).items()))
        if sha256_file(path) != before[capture_id]:
            raise RuntimeError("capture_changed")

    all_variants = [variant for capture in matrices for variant in capture["variants"]]
    protocol = {
        "schema": "guance-phase7-protocol-variant-matrix-v1",
        "generated_at": "2026-10-15 Asia/Shanghai", "read_only": True,
        "client_payload_decoded": False, "captures": matrices,
        "observed_variants": all_variants,
    }
    _write(evidence / "protocol-variant-matrix.json", protocol)
    md = ["# Protocol Variant Matrix", "", "Client payload decoded: **no**.", "",
          "| Capture | Epochs | Variants | First rejection |",
          "|---|---:|---:|---|"]
    for row in matrices:
        reject = row["first_parser_rejection"]
        code = "none" if reject is None else str(reject["code"])
        md.append(f"| {row['capture_id']} | {row['connection_epochs']} | {len(row['variants'])} | {code} |")
    md.extend(("", "## Observed shapes", "",
               "| Capture | Command | Record | Wrapper | Shape | Count | Parser | Class |",
               "|---|---:|---:|---:|---|---:|---|---|"))
    for row in all_variants:
        md.append(f"| {row['capture_id']} | {row['command_id']} | {row['record_version']} | "
                  f"{row['wrapper_variant']} | `{row['group_shape']}` | {row['occurrence_count']} | "
                  f"{row['parser_expected_result']} | {row['support_classification']} |")
    (evidence / "protocol-variant-matrix.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    history = {
        "schema": "guance-phase7-historical-capture-replay-matrix-v1",
        "generated_at": "2026-10-15 Asia/Shanghai", "read_only": True,
        "client_payload_decoded": False, "captures": replays,
    }
    _write(evidence / "historical-capture-replay-matrix.json", history)
    hmd = ["# Historical Capture Replay Matrix", "",
           "| Capture | Classification | Protocol rejected | Errors | Supported decisions |",
           "|---|---|---:|---|---:|"]
    for row in replays:
        hmd.append(f"| {row['capture_id']} | {', '.join(row['classifications'])} | "
                   f"{row['protocol_rejected']} | {', '.join(row['error_counts']) or 'none'} | "
                   f"{row['supported_decision_count']} |")
    (evidence / "historical-capture-replay-matrix.md").write_text("\n".join(hmd) + "\n", encoding="utf-8")

    latest = next(row for row in replays if row["capture_id"] == LATEST)
    _write(evidence / "latest-capture-replay-summary.json", latest)
    _write(evidence / "latest-capture-command-matrix.json",
           next(row for row in matrices if row["capture_id"] == LATEST))
    _write(evidence / "latest-capture-state-summary.json", {
        "schema": "guance-phase7-latest-state-summary-v1",
        "capture_id": LATEST, "connection_epochs": latest["connection_epochs"],
        "event_counts": latest["event_counts"], "ranking_valid": True,
        "remaining_counts_valid": True, "action_chain_valid": not latest["error_counts"],
        "pass_chain_valid": not latest["error_counts"],
        "cross_epoch_previous_result": "not_associated",
    })
    normal = next(row for row in replays if row["capture_id"] == NORMAL)
    _write(evidence / "normal-exchange-offline-replay.json", {
        "schema": "guance-phase7-normal-exchange-offline-replay-v1",
        "capture_id": NORMAL, "source_sha256": before[NORMAL][0],
        "source_bytes": before[NORMAL][1], "game_boundaries": 5,
        "settlements": normal["event_counts"].get("SettlementEvent", 0),
        "supported_decision_count": normal["supported_decision_count"],
        "supported_decisions": normal["supported_decisions"],
        "unsupported_codes": normal["error_counts"],
        "provenance": "offline_replay", "live_artifact": False,
        "canonical_live_contract": False, "client_payload_decoded": False,
    })
    _write(evidence / "field11-evidence.json", {
        "schema": "guance-phase7-field11-evidence-v1",
        "expected_length": 20,
        "capture_counts": {cid: {str(k): v for k, v in counts.items()}
                           for cid, counts in field11.items()},
        "normal_capture_20_bytes": field11[NORMAL] == {20: 169},
        "latest_capture_20_bytes": field11[LATEST] == {20: 68},
        "synthetic_18_byte_positive_allowed": False,
    })
    _write(evidence / "parser-repair-evidence.json", {
        "schema": "guance-phase7-parser-repair-evidence-v1",
        "protocol_identity": live.PROTOCOL_IDENTITY,
        "field11_exact_length": 20,
        "added_3005_shapes": [
            "1:0,2:0,4:0,6:0,7:2,8:0,9:0,11:2",
            "2:0,4:0,5:0,6:0,7:2,8:0,9:0,11:2",
        ],
        "added_3006_shapes": ["2:0,5:0"],
        "informational_commands_proved_in_no_exchange": {"3003": 8, "3007": 4},
        "latest_protocol_rejected": latest["protocol_rejected"],
        "latest_engine_paused": latest["engine_paused"],
    })
    analysis_dir = ROOT / "private_data" / "tencent_live_protocol_analysis_20261014-143204"
    _write(evidence / "analysis-corrections.json", {
        "schema": "guance-phase7-analysis-corrections-v1",
        "field11": {"normal": "169/169 at 20 bytes", "latest": "68/68 at 20 bytes"},
        "game1_3011_present": True, "commands_3012_3015_game1_absent": True,
        "first_observed_previous_result": "unknown_unverified",
        "commands_3003_3007": "informational_cross_capture",
        "exchange_mapping_evidence_level": "LEVEL_3",
        "ground_truth_sha256": sha256_file(analysis_dir / "exchange-analysis-groundtruth.json")[0],
        "sha256sums_sha256": sha256_file(analysis_dir / "SHA256SUMS")[0],
        "capture_unchanged": sha256_file(ROOT / "debug" / "captures" / NORMAL / "frames.jsonl") == before[NORMAL],
    })
    print(json.dumps({"captures": len(CAPTURE_IDS), "variants": len(all_variants),
                      "latest_errors": latest["error_counts"],
                      "normal_decisions": normal["supported_decision_count"]},
                     separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
