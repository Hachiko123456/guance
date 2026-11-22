"""Phase 6 acceptance for the DanKS V3 danks-cpu Windows package.

Drives the ACTUAL frozen package: worker-subprocess model flow (load, real
recommend, forged-history negative, restart, per-shape latency margin),
packaged catalog bundle identity, and first-launch catalog seeding through
the frozen smoke entry.  Evidence is written as one JSON report.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from guance.contracts import (CanonicalPublicGameView,  # noqa: E402
                              CurrentWinningDeclarationFact, DeclaredFacts,
                              PublicRecommendationRequestV2, RequestIdentity)
from guance.infrastructure.model.client import InferenceClient  # noqa: E402

PHASE1 = ROOT / "tests/fixtures/danks_v3"
SAMPLES = PHASE1 / "samples_32.json"


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _facts() -> DeclaredFacts:
    return DeclaredFacts(schema="Guandan.DeclaredFacts.v2",
                         current_winning_declaration=CurrentWinningDeclarationFact(
                             status="absent"),
                         history_origin="synthetic_normal_entry",
                         public_history_complete="known", history_complete_value=True,
                         transport_contiguous="known", transport_value=True)


def _frozen_client(exe: Path, bundle: Path, threads: int = 1) -> InferenceClient:
    client = InferenceClient(str(bundle), on_result=lambda r: None,
                             on_status=lambda s: None, timeout_seconds=30.0,
                             threads=threads, requested_device="cpu")
    # Point the production launch spec at the frozen executable: the package
    # IS the worker when frozen (spec branch for sys.frozen).
    def frozen_spec():
        env = os.environ.copy()
        env["GUANCE_DANKS_CHECKPOINT"] = ""
        return ([str(exe), "--model-worker", "--threads", str(threads),
                 "--device", "cpu"], env, 0x08000000)  # CREATE_NO_WINDOW
    client._worker_process_spec = frozen_spec  # type: ignore[method-assign]
    return client


def _collect_status(client: InferenceClient, want: str, timeout: float) -> list[dict]:
    statuses: list[dict] = []
    client.on_status = statuses.append  # type: ignore[method-assign]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready = [s for s in statuses if s.get("model_status") == want]
        if ready:
            return ready
        time.sleep(0.1)
    return statuses


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--candidate", type=int, default=1)
    args = parser.parse_args()
    package = args.package_root.resolve()
    exe = package / "Guance.exe"
    evidence = args.evidence_root.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    if not exe.is_file():
        raise SystemExit(f"invalid package root: {package}")

    internal = package / "_internal"
    bundle = internal / "data" / "models" / "catalog" / "danks-v3-top10-v1"
    report: dict = {"candidate": args.candidate, "package_root": str(package)}

    # 1. Packaged bundle identity vs the source tree.
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    weights = bundle / "weights" / "top10_selector_v3_online.pt"
    report["bundle"] = {
        "manifest_sha256": _sha256(bundle / "manifest.json"),
        "weights_sha256": _sha256(weights),
        "weights_bytes": weights.stat().st_size,
        "artifact_matches_manifest": _sha256(weights) == manifest["artifact_sha256"],
    }

    # 2. Frozen worker model flow: load, recommend, negative, latency, restart.
    samples = json.loads(SAMPLES.read_text(encoding="utf-8"))
    opening = next(row for row in samples["samples"]
                   if row["sample_id"] == "danks-p1-open-01")
    follow = next(row for row in samples["samples"]
                  if row["sample_id"] == "danks-p1-mid-00")
    client = _frozen_client(exe, bundle)
    checks: dict = {}
    try:
        client.submit(PublicRecommendationRequestV2(
            RequestIdentity("p6-warmup", "s", "g", 1),
            CanonicalPublicGameView.from_dict(opening["observation"]), _facts()))
        ready = _collect_status(client, "ready", 240.0)
        checks["worker_ready"] = bool(ready)
        if ready:
            checks["ready_metadata"] = {
                "provider": ready[-1].get("metadata", {}).get("provider"),
                "actual_device": ready[-1].get("metadata", {}).get("actual_device"),
            }

        # Out-of-set warm-up (user methodology): one big-batch opening whose
        # hand differs from every registered sample, so the one-off pool
        # spawn cost lands outside the measured 38.
        warm = dict(opening["observation"])
        hand = list(warm["hand"])
        for index, code in enumerate(hand):
            suit, rank = code >> 4, code & 15
            alt_suit = 4 - suit if suit in (1, 2, 3) else suit
            candidate = (alt_suit << 4) | rank
            if candidate != code and candidate not in hand:
                hand[index] = candidate
                break
        warm["hand"] = hand
        warm_view = CanonicalPublicGameView.from_dict(warm)
        received: list = []
        done = threading.Event()
        client.on_result = lambda r: (received.append(r), done.set())  # type: ignore[method-assign]
        started = time.perf_counter()
        client.submit(PublicRecommendationRequestV2(
            RequestIdentity("p6-warm-big", warm_view.session_id,
                            warm_view.game_id, warm_view.state_version),
            warm_view, _facts()))
        warm_ok = done.wait(90.0)
        checks["warmup_out_of_set"] = {
            "delivered": bool(warm_ok and received),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        }

        # The user gate: all 38 frozen samples through the frozen worker with
        # per-shape budgets (follow 2 s / free lead 6 s); >= 37/38 timely.
        from guance.public_state_contract import PublicDeclaration

        def facts_for_row(row):
            obs = row["observation"]
            cf = row.get("current_winning_declaration_fact") or {}
            if not obs["free_lead"] and cf.get("status") == "known":
                return DeclaredFacts(schema="Guandan.DeclaredFacts.v2",
                    current_winning_declaration=CurrentWinningDeclarationFact(
                        status="known",
                        declaration=PublicDeclaration.from_dict(cf["declaration"])),
                    history_origin="synthetic_normal_entry",
                    public_history_complete="known", history_complete_value=True,
                    transport_contiguous="known", transport_value=True)
            return _facts()

        samples_json = follow["observation"]
        facts = facts_for_row(follow)
        rows = []
        timely = 0
        for row in samples["samples"]:
            obs = row["observation"]
            budget = 6000.0 if obs["free_lead"] else 2000.0
            view = CanonicalPublicGameView.from_dict(obs)
            received.clear()
            done.clear()
            started = time.perf_counter()
            client.submit(PublicRecommendationRequestV2(
                RequestIdentity(f"p6-38-{row['sample_id']}", view.session_id,
                                view.game_id, view.state_version),
                view, facts_for_row(row)))
            ok = done.wait(90.0)
            elapsed_ms = (time.perf_counter() - started) * 1000
            sample_timely = bool(ok and received and received[-1].status == "ready" and elapsed_ms <= budget)
            timely += 1 if sample_timely else 0
            rows.append({"sample_id": row["sample_id"], "free_lead": obs["free_lead"],
                         "elapsed_ms": round(elapsed_ms, 1), "budget_ms": budget,
                         "timely": sample_timely})
        checks["latency_38"] = {
            "timely": timely, "total": len(rows),
            "ratio": round(timely / len(rows), 4) if rows else 0.0,
            "gate": ">=37/38 per-shape budgets through the frozen worker",
            "gate_passed": timely >= 37,
            "late_rows": [r for r in rows if not r["timely"]],
            "max_elapsed_ms": max((r["elapsed_ms"] for r in rows), default=0.0),
        }

        # Negative: forged mid-trick history (deleted PASS, renumbered) must
        # reject the position at the frozen worker entry.
        raw = dict(follow["observation"])
        raw["public_history"] = [dict(a) for a in raw["public_history"]]
        current = [a for a in raw["public_history"] if a["trick"] == raw["trick"]]
        victim = next(a for a in current[1:] if a["kind"] == "pass")
        raw["public_history"] = [a for a in raw["public_history"]
                                 if a["index"] != victim["index"]]
        for new_index, action in enumerate(raw["public_history"], start=1):
            action["index"] = new_index
        received.clear()
        done.clear()
        client.submit(PublicRecommendationRequestV2(
            RequestIdentity("p6-forged", raw["session_id"], raw["game_id"],
                            raw["state_version"]),
            CanonicalPublicGameView.from_dict(raw), facts))
        rejected = received[-1] if done.wait(30.0) and received else None
        checks["negative_forged_pass"] = {
            "errored": bool(rejected and rejected.status == "unavailable"
                            and rejected.error_code == "MODEL_INPUT_UNSUPPORTED" and not rejected.candidates),
            "error_code": rejected.error_code if rejected else None,
        }

        # Restart: close and reload through a fresh client.
        client.close()
        client = _frozen_client(exe, bundle)
        received.clear()
        done.clear()
        client.on_result = lambda r: (received.append(r), done.set())  # type: ignore[method-assign]
        client.submit(PublicRecommendationRequestV2(
            RequestIdentity("p6-restart", samples_json["session_id"],
                            samples_json["game_id"], samples_json["state_version"]),
            CanonicalPublicGameView.from_dict(samples_json), facts))
        checks["restart"] = {"delivered": bool(done.wait(120.0) and received and received[-1].status == "ready")}
    finally:
        client.close()

    # 3. First-launch catalog seeding through the frozen smoke entry.
    seed_tmp = Path(tempfile.mkdtemp(prefix="guance-p6-seed-"))
    try:
        env = os.environ.copy()
        env["GUANCE_DATA_DIR"] = str(seed_tmp)
        smoke = subprocess.run(
            [str(exe), "--frozen-smoke", "--smoke-report",
             str(seed_tmp / "smoke.json"), "--exit-after", "12"],
            env=env, capture_output=True, timeout=180)
        smoke_report_path = seed_tmp / "smoke.json"
        smoke_report = {}
        if smoke_report_path.is_file():
            try:
                smoke_report = json.loads(smoke_report_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                smoke_report = {}
        seeded_manifest = seed_tmp / "models" / "catalog" / "danks-v3-top10-v1" / "manifest.json"
        report["smoke"] = {
            "exit_code": smoke.returncode,
            "smoke_ok": bool(smoke_report.get("status")) and smoke_report.get("status") == "passed",
            "stdout_tail": smoke.stdout.decode("utf-8", "replace")[-400:],
        }
        report["catalog_seeding"] = {
            "manifest_seeded": seeded_manifest.is_file(),
            "manifest_matches": (seeded_manifest.is_file()
                                 and _sha256(seeded_manifest) == report["bundle"]["manifest_sha256"]),
        }
    finally:
        shutil.rmtree(seed_tmp, ignore_errors=True)

    report["checks"] = checks
    passed = (
        report["bundle"]["artifact_matches_manifest"]
        and checks.get("worker_ready") and checks.get("latency_38", {}).get("gate_passed")
        and checks.get("negative_forged_pass", {}).get("errored")
        and checks.get("restart", {}).get("delivered")
        and report.get("smoke", {}).get("smoke_ok")
        and report.get("catalog_seeding", {}).get("manifest_seeded")
        and report.get("catalog_seeding", {}).get("manifest_matches")
    )
    report["passed"] = bool(passed)
    out = evidence / "danks_package_acceptance.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("passed", "checks", "catalog_seeding")},
                     ensure_ascii=False, indent=1))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
