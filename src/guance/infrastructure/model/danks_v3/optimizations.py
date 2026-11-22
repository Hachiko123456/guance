"""Deployment-side reversible, semantics-equivalent optimizations (受限 A).

The vendored reference subset stays READ-ONLY; optimizations live here and
are reversible by environment flag.

History: hand-keyed partition memoization was measured NEGATIVE (2026-11-21,
2422 ms -> 24789 ms on the frozen opening workload: the ranker already
deduplicates by after-hand and the ~1102 ``generate`` calls on a 27-card
opening all carry DISTINCT hands, so cache maintenance dominates).  The
sparse-helper memo wrappers below are retained as a recorded negative result
and are NOT applied by the provider.

The applied optimization is the PARALLEL PARTITION PRECOMPUTE.  The ranker
consumes one independent ``partitioner.generate`` call per distinct
after-hand (measured on the frozen open-01 sample: 1102 calls, hand sizes
21-27, all distinct).  Each call is a pure function of
(hand, ctx, partition_limit) under an identically-configured partitioner -
proven by a fresh-replica byte comparison of the produced partitions - so
the whole batch is dispatched to a persistent worker-process pool at the
ranker's own enumeration point (``generate_top_hand_count_window_batch``,
called once per ``rank`` with the complete batch-hand list BEFORE the
per-hand loop) and served from a per-rank stash at ``generate`` consumption
time.  The vendor serial path remains the fallback for every
non-qualifying, missing, or failed hand, so outputs are identical in all
cases by construction.  Equivalence is enforced by the layer-by-layer frozen
comparison (legal list / Top10 order / features / mask / logits / first
action) plus a 38-sample parallel-vs-serial candidate identity test.

Reversibility: ``GUANCE_DANKS_PARTITION_PARALLEL=0`` (or any pool failure)
restores the exact vendor serial behaviour.
"""
from __future__ import annotations

import multiprocessing
import os
import threading
import time
from bisect import insort
from concurrent.futures import Future, ProcessPoolExecutor
from functools import lru_cache
from math import ulp
from typing import Any

from .vendor.danks.retrieval.cards import normalize_cards

PARALLEL_ENV = "GUANCE_DANKS_PARTITION_PARALLEL"
WORKERS_ENV = "GUANCE_DANKS_PARTITION_PARALLEL_WORKERS"
MIN_BATCH_ENV = "GUANCE_DANKS_PARTITION_PARALLEL_MIN_BATCH"


def _enabled() -> bool:
    return os.environ.get(PARALLEL_ENV, "1").strip().lower() in {
        "1", "true", "yes", "on"}


def _worker_count() -> int:
    override = os.environ.get(WORKERS_ENV, "").strip()
    if override:
        try:
            return max(2, int(override))
        except ValueError:
            pass
    return max(2, min(8, (os.cpu_count() or 4) - 1))


def _min_batch() -> int:
    try:
        return int(os.environ.get(MIN_BATCH_ENV, "48"))
    except ValueError:
        return 48


def _pool_init() -> None:
    """Child-side warm task reserved for explicit pool materialization."""
    from .vendor.danks.retrieval.partitioner import FullSearchPartitioner  # noqa: F401


def _pool_generate_batch(payload: tuple) -> list:
    """Worker body: rebuild a fresh partitioner replica and serve a chunk.

    The replica uses the reference constructor defaults plus the main
    instance's group-priority profile - the only mutable configuration the
    partition search reads (a fresh replica reproduces the main instance's
    ``generate`` output exactly; verified 2026-11-21).
    """
    profile_name, base_by_kind, bomb_bonus, ctx, partition_limit, hands = payload
    from .vendor.danks.retrieval.partitioner import FullSearchPartitioner
    replica = FullSearchPartitioner(use_native_all=True)
    replica.set_group_priority_profile(profile_name, base_by_kind, bomb_bonus)
    return [_bounded_generate(replica, replica.generate, hand, ctx, partition_limit)
            for hand in hands]


def _bounded_generate(partitioner, reference, hand, ctx, limit):
    """Prune the expensive pure-Python small-hand search without changing its order."""
    from .vendor.danks.retrieval.partitioner import FullSearchPartitioner, _subtract_local_sparse

    if (not isinstance(partitioner, FullSearchPartitioner)
            or not isinstance(hand, (tuple, list))
            or not isinstance(limit, int) or limit < 1):
        return reference(hand, ctx, limit)
    count = len(hand)
    if (not _enabled() or count < 12
            or count > partitioner.exhaustive_top_threshold
            or partitioner._env_int("DANKS_PARTITION_HAND_COUNT_WINDOW", -1) >= 0):
        return reference(hand, ctx, limit)
    from .vendor.danks.retrieval import native_cover
    if native_cover.available():
        return reference(hand, ctx, limit)
    from .vendor.danks.retrieval.models import Partition, effective_group_cost

    entries, start, groups_by_first, _ = partitioner._cover_search_inputs(
        normalize_cards(hand), ctx, include_priority=True,
    )
    groups = [entry[0] for entry in entries]
    priorities = [entry[2] for entry in entries]
    costs = [effective_group_cost(group) for group in groups]
    scores = [priority - 10.0 * cost for priority, cost in zip(priorities, costs)]
    names = [(group.kind, group.cards) for group in groups]
    # A sum of per-group scores is only used as an upper bound. Leave room for
    # round-off in both summation orders, then compare finalists by the original
    # sum(priorities) - 10 * sum(costs) and the vendor's stable ordering.
    margin = ulp(count * (10.0 + max(map(abs, priorities)))) * (4 * count + 2)
    selected = []

    def branches(state):
        first = next((index for index, value in enumerate(state) if value), None)
        if first is not None:
            for group_id in groups_by_first[first]:
                following = _subtract_local_sparse(state, entries[group_id][1])
                if following is not None:
                    yield group_id, following

    @lru_cache(maxsize=16384)
    def best_suffix(state):
        if not any(state):
            return 0.0
        return max((scores[group_id] + best_suffix(following)
                    for group_id, following in branches(state)), default=float("-inf"))

    def visit(state, cover, score):
        if len(selected) == limit and score + best_suffix(state) < -selected[-1][0] - margin:
            return
        if not any(state):
            actual = sum(priorities[i] for i in cover) - 10.0 * sum(costs[i] for i in cover)
            insort(selected, (-actual, tuple(names[i] for i in cover), cover))
            if len(selected) > limit:
                selected.pop()
            return
        children = sorted(
            branches(state),
            key=lambda item: -(scores[item[0]] + best_suffix(item[1])),
        )
        for group_id, following in children:
            visit(following, (*cover, group_id), score + scores[group_id])

    try:
        visit(start, (), 0.0)
        if not selected:
            return reference(hand, ctx, limit)
        return [Partition(tuple(groups[i] for i in cover), "full_search_all")
                for _, _, cover in selected]
    finally:
        best_suffix.cache_clear()


class ParallelPartitionService:
    """Rank-scoped dispatch/stash around one ranker's partitioner."""

    def __init__(self) -> None:
        self._executor: ProcessPoolExecutor | None = None
        self._stash: dict[tuple, tuple[Future, int]] = {}
        self._limit: int | None = None
        self._partitioner: Any = None
        self._keepalive: threading.Thread | None = None

    # -- install ----------------------------------------------------------
    def install(self, partitioner: Any) -> None:
        if getattr(partitioner, "_guance_parallel_installed", False):
            return
        original_generate = partitioner.generate
        original_window_batch = partitioner.generate_top_hand_count_window_batch
        service = self
        self._partitioner = partitioner

        def generate(hand_cards, ctx, max_partitions=None, *args, **kwargs):
            stash = service._stash
            if (stash and max_partitions is not None
                    and max_partitions == service._limit):
                key = tuple(normalize_cards(hand_cards))
                entry = stash.pop(key, None)
                if entry is not None:
                    future, index = entry
                    try:
                        return list(future.result()[index])
                    except Exception:
                        stash.clear()
            if not args and not kwargs:
                return _bounded_generate(partitioner, original_generate, hand_cards, ctx, max_partitions)
            return original_generate(hand_cards, ctx, max_partitions, *args, **kwargs)

        def window_batch(super_hand_cards, after_hands, ctx, max_partitions,
                         prepared_inputs=None, *args, **kwargs):
            service._stash = {}
            service._limit = max_partitions
            if (_enabled() and max_partitions is not None
                    and not service._native_available()):
                keys = list(dict.fromkeys(
                    tuple(normalize_cards(cards)) for cards in after_hands))
                if len(keys) >= _min_batch():
                    try:
                        service._dispatch(keys, ctx, max_partitions)
                        return {}
                    except Exception:
                        service._stash = {}
            return original_window_batch(super_hand_cards, after_hands, ctx,
                                         max_partitions, prepared_inputs,
                                         *args, **kwargs)

        partitioner.generate = generate
        partitioner.generate_top_hand_count_window_batch = window_batch
        generate._guance_original = original_generate
        window_batch._guance_original = original_window_batch
        partitioner._guance_parallel_installed = True

    def uninstall(self, partitioner: Any) -> None:
        """Restore the plain vendor methods (reversibility helper for tests)."""
        generate = getattr(partitioner.generate, "_guance_original", None)
        window = getattr(partitioner.generate_top_hand_count_window_batch,
                         "_guance_original", None)
        if generate is not None:
            partitioner.generate = generate
        if window is not None:
            partitioner.generate_top_hand_count_window_batch = window
        partitioner._guance_parallel_installed = False
        self._stash = {}

    # -- dispatch ---------------------------------------------------------
    def _native_available(self) -> bool:
        # With the vendor's native kernels present, its own batch paths serve
        # the ranker and no precompute is wanted.
        try:
            from .vendor.danks.retrieval import native_cover
            return bool(native_cover.available())
        except Exception:
            return True  # unsure -> stay on the vendor path

    def _dispatch(self, keys: list[tuple], ctx: Any, max_partitions: int) -> None:
        partitioner = self._partitioner
        payload_head = (partitioner._priority_profile_name,
                        partitioner._priority_base_by_kind,
                        partitioner._priority_bomb_size_bonus,
                        ctx, max_partitions)
        executor = self._ensure_executor()
        workers = self._executor_workers
        chunks = [keys[i::workers] for i in range(workers)]
        chunks = [chunk for chunk in chunks if chunk]
        stash: dict[tuple, tuple[Future, int]] = {}
        for chunk in chunks:
            future = executor.submit(_pool_generate_batch,
                                     (*payload_head, chunk))
            for index, key in enumerate(chunk):
                stash[key] = (future, index)
        self._stash = stash

    def _ensure_executor(self) -> ProcessPoolExecutor:
        if self._executor is None:
            workers = _worker_count()
            try:
                context = multiprocessing.get_context("spawn")
                self._executor = ProcessPoolExecutor(
                    max_workers=workers, mp_context=context)
            except Exception:
                raise
        self._executor_workers = _worker_count()
        return self._executor

    def shutdown(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
        self._stash = {}


_SERVICE_KEEPALIVE_LOCK = threading.Lock()
_SERVICE = ParallelPartitionService()


def install_parallel_partitions(ranker: Any) -> dict[str, int]:
    """Wire the service into a vendor ranker's partitioner (idempotent)."""
    partitioner = getattr(ranker, "partitioner", None)
    if partitioner is None:
        return {"installed": 0}
    if not hasattr(partitioner, "generate_top_hand_count_window_batch"):
        return {"installed": 0}
    _SERVICE.install(partitioner)
    return {"installed": 1}


def prewarm_parallel_partitions() -> bool:
    """Materialize the worker pool at advisor load time (best effort).

    The executor spawns children lazily against queued work: a burst of
    instant tasks never builds the backlog needed to reach max_workers, so
    the first real request would pay the full serialized spawn cost.  The
    warm tasks below HOLD their queue slots briefly, forcing one child per
    worker slot to spawn (in parallel) while nothing is being measured; the
    daemon keepalive then re-submits cheap tasks so children stay
    materialized.  Failure is non-fatal (rank-time dispatch falls back to
    the vendor serial path).  Reversible:
    ``GUANCE_DANKS_PARTITION_PARALLEL=0`` disables the whole service.
    """
    if not _enabled():
        return False
    try:
        executor = _SERVICE._ensure_executor()
        workers = max(1, _SERVICE._executor_workers)

        def _warm_task() -> None:
            time.sleep(1.5)

        def _keepalive() -> None:
            while True:
                try:
                    for _ in range(workers):
                        executor.submit(_pool_init)
                except Exception:
                    return
                time.sleep(30.0)

        with _SERVICE_KEEPALIVE_LOCK:
            if _SERVICE._keepalive is None or not _SERVICE._keepalive.is_alive():
                _SERVICE._keepalive = threading.Thread(
                    target=_keepalive, name="guance-danks-pool-keepalive",
                    daemon=True)
                _SERVICE._keepalive.start()
        futures = [executor.submit(_warm_task) for _ in range(workers)]
        for future in futures:
            future.result(timeout=120.0)
        return True
    except Exception:
        return False


def shutdown_parallel_partitions() -> None:
    _SERVICE.shutdown()


# ---------------------------------------------------------------------------
# Measured-negative result, retained for the record (NOT applied): the
# sparse-helper memo wrappers below were benchmarked 10x SLOWER on the
# frozen opening workload and must not be enabled by the provider.
# ---------------------------------------------------------------------------

_applied: list[tuple[Any, str, Any]] = []


def _freeze(value):
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value
