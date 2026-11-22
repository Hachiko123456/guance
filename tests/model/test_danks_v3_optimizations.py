"""Parallel partition precompute service tests (受限 A reversible optimization).

The service dispatches the ranker's per-after-hand ``generate`` calls to a
worker-process pool and serves identical results from a per-rank stash;
every failure path falls back to the vendor serial computation, so outputs
must be identical in all cases.  These tests prove that identity directly
against a plain serial partitioner, plus the fallback/flag behaviour.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from guance.infrastructure.model.danks_v3 import optimizations
from guance.infrastructure.model.danks_v3.vendor.danks.retrieval.context import \
    RetrievalContext
from guance.infrastructure.model.danks_v3.vendor.danks.retrieval.partitioner import \
    FullSearchPartitioner

ROOT = Path(__file__).resolve().parents[2]
PHASE1 = ROOT / "tests/fixtures/danks_v3"
SAMPLES = PHASE1 / "samples_32.json"
CHECKPOINT_PATH = Path(os.environ.get(
    "GUANCE_DANKS_CHECKPOINT", r"E:\Downloads\top10_selector_v3_online.pt"))
_real_checkpoint = pytest.mark.skipif(
    not CHECKPOINT_PATH.is_file() or not SAMPLES.is_file(),
    reason="frozen checkpoint or samples not on this host")


def _synthetic_hands(count: int) -> list[tuple[str, ...]]:
    base = ["S3", "S4", "S5", "S6", "S7", "S8", "S9", "ST", "SJ", "SQ",
            "SK", "SA", "S2", "H3", "H4", "H5", "H6", "H7", "H8", "H9",
            "HT", "HJ", "HQ", "HK", "C3", "C4", "C5", "C6", "C7"]
    # Remove a different card per hand so every hand is distinct (the
    # dispatch only fires when the deduplicated batch exceeds the minimum).
    hands = [tuple(base[:i] + base[i + 1:]) for i in range(count)]
    assert len(set(hands)) == count
    return hands


def _signature(partitions) -> list:
    return [(tuple(tuple(g.cards) if hasattr(g, "cards") else ()
                   for g in partition.groups), partition.mode)
            for partition in partitions]


@pytest.fixture()
def service(monkeypatch):
    """A fresh service bound to a fresh partitioner; pool torn down after."""
    monkeypatch.setenv(optimizations.MIN_BATCH_ENV, "10")
    optimizations._SERVICE.shutdown()
    partitioner = FullSearchPartitioner(use_native_all=True)
    optimizations._SERVICE.install(partitioner)
    yield partitioner
    optimizations._SERVICE.shutdown()


def _serial_signature(hands, ctx, limit) -> dict:
    serial = FullSearchPartitioner(use_native_all=True)
    serial.set_group_priority_profile("default", serial._priority_base_by_kind,
                                      serial._priority_bomb_size_bonus)
    return {tuple(hand): _signature(serial.generate(list(hand), ctx, limit))
            for hand in hands}


def test_parallel_results_match_serial(service):
    hands = _synthetic_hands(24)
    ctx = RetrievalContext()
    serial = _serial_signature(hands, ctx, 8)
    # The enumeration hook dispatches the batch; consumption then serves the
    # identical results from the stash.
    assert service.generate_top_hand_count_window_batch(
        hands[0], hands, ctx, 8) == {}
    got = {tuple(hand): _signature(service.generate(list(hand), ctx, 8))
           for hand in hands}
    assert got == serial
    assert not optimizations._SERVICE._stash


def test_dispatch_failure_falls_back_serial(service, monkeypatch):
    hands = _synthetic_hands(24)
    ctx = RetrievalContext()
    serial = _serial_signature(hands, ctx, 8)
    monkeypatch.setattr(optimizations._SERVICE, "_dispatch",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("pool down")))
    service.generate_top_hand_count_window_batch(hands[0], hands, ctx, 8)
    got = {tuple(hand): _signature(service.generate(list(hand), ctx, 8))
           for hand in hands}
    assert got == serial


def test_consume_failure_falls_back_serial(service):
    """A batch whose payload cannot reach the workers must still produce the
    serial results through the per-hand fallback."""
    hands = _synthetic_hands(24)
    ctx = RetrievalContext()
    serial = _serial_signature(hands, ctx, 8)

    class Unpicklable:
        def __reduce__(self):  # pragma: no cover - failure injection
            raise TypeError("not picklable")

    service.generate_top_hand_count_window_batch(
        hands[0], hands, Unpicklable(), 8)
    got = {tuple(hand): _signature(service.generate(list(hand), ctx, 8))
           for hand in hands}
    assert got == serial


def test_small_batch_stays_serial(service):
    hands = _synthetic_hands(5)
    ctx = RetrievalContext()
    serial = _serial_signature(hands, ctx, 8)
    service.generate_top_hand_count_window_batch(hands[0], hands, ctx, 8)
    assert not optimizations._SERVICE._stash
    got = {tuple(hand): _signature(service.generate(list(hand), ctx, 8))
           for hand in hands}
    assert got == serial


def test_env_disable_keeps_serial(monkeypatch):
    monkeypatch.setenv(optimizations.PARALLEL_ENV, "0")
    monkeypatch.setenv(optimizations.MIN_BATCH_ENV, "10")
    optimizations._SERVICE.shutdown()
    partitioner = FullSearchPartitioner(use_native_all=True)
    optimizations._SERVICE.install(partitioner)
    hands = _synthetic_hands(24)
    ctx = RetrievalContext()
    serial = _serial_signature(hands, ctx, 8)
    partitioner.generate_top_hand_count_window_batch(hands[0], hands, ctx, 8)
    assert not optimizations._SERVICE._stash
    got = {tuple(hand): _signature(partitioner.generate(list(hand), ctx, 8))
           for hand in hands}
    assert got == serial


def test_generate_all_never_serves_stash(service, monkeypatch):
    # The stash-serving guard is what is under test here; force the parallel
    # path on even when an inherited environment disabled the flag.
    monkeypatch.setenv(optimizations.PARALLEL_ENV, "1")
    hands = _synthetic_hands(24)
    ctx = RetrievalContext()
    # generate_all exhaustively enumerates partitions - keep the hands small.
    small = [hand[:8] for hand in hands]
    serial = _serial_signature(small, ctx, None)
    service.generate_top_hand_count_window_batch(hands[0], hands, ctx, 8)
    assert optimizations._SERVICE._stash
    got = {tuple(hand): _signature(service.generate(list(hand), ctx, None))
           for hand in small}
    assert got == serial
    assert optimizations._SERVICE._stash


def test_uninstall_restores_vendor_methods(service):
    hands = _synthetic_hands(24)
    ctx = RetrievalContext()
    serial = _serial_signature(hands, ctx, 8)
    optimizations._SERVICE.uninstall(service)
    assert not getattr(service, "_guance_parallel_installed", False)
    got = {tuple(hand): _signature(service.generate(list(hand), ctx, 8))
           for hand in hands}
    assert got == serial


@pytest.mark.parametrize("level", ["5", "A", "2"])
def test_bounded_exhaustive_search_preserves_rank_and_tie_order(level, monkeypatch):
    monkeypatch.setenv(optimizations.PARALLEL_ENV, "1")
    monkeypatch.setenv("DANKS_PARTITION_HAND_COUNT_WINDOW", "-1")
    base = ("S3", "S4", "S5", "S6", "S7", "H5", "H5", "C3", "D4",
            "C5", "D6", "D7", "D8", "C8", "C9", "C9", "BJ", "RJ")
    ctx = RetrievalContext(cur_rank=level, remaining_detail={"RJ": 0})
    reference = FullSearchPartitioner(use_native_all=True)
    bounded = FullSearchPartitioner(use_native_all=True)
    for hand in (base, base[:-1], base[:13], base[2:]):
        expected = reference.generate(hand, ctx, 8)
        assert len(expected) == 8
        assert optimizations._bounded_generate(bounded, bounded.generate, hand, ctx, 8) == expected


@_real_checkpoint
def test_all_38_samples_parallel_serial_candidate_identity(tmp_path: Path):
    """The heavyweight equivalence proof: every frozen sample's full display
    candidates (kind, cards, exact score) must be identical with the
    optimization enabled and disabled."""
    import time as _time
    from test_danks_v3_provider import (_facts, build_bundle, DanksV3Advisor)
    from guance.contracts import (CanonicalPublicGameView,
                                  CurrentWinningDeclarationFact)
    from guance.public_state_contract import PublicDeclaration

    samples = json.loads(SAMPLES.read_text(encoding="utf-8"))

    def facts_for(row):
        obs = row["observation"]
        cf = row.get("current_winning_declaration_fact") or {}
        if not obs["free_lead"] and cf.get("status") == "known":
            return _facts(current_winning_declaration=CurrentWinningDeclarationFact(
                status="known",
                declaration=PublicDeclaration.from_dict(cf["declaration"])))
        return _facts(current_winning_declaration=CurrentWinningDeclarationFact(
            status="absent"))

    def run_all(flag: str) -> dict:
        monkey = os.environ
        monkey[optimizations.PARALLEL_ENV] = flag
        bundle = build_bundle(CHECKPOINT_PATH, tmp_path / f"bundle-{flag}")
        advisor = DanksV3Advisor(bundle, requested_device="cpu")
        advisor.load()
        out = {}
        for row in samples["samples"]:
            view = CanonicalPublicGameView.from_dict(row["observation"])
            result = advisor.recommend(view, row["sample_id"],
                                       declared_facts=facts_for(row))
            out[row["sample_id"]] = [(c.kind, tuple(c.cards), c.score)
                                     for c in result.candidates]
        advisor.close()
        return out

    try:
        serial = run_all("0")
        parallel = run_all("1")
    finally:
        optimizations._SERVICE.shutdown()
    assert parallel == serial
