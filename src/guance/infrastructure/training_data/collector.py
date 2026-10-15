"""Synchronous, single-owner semantic builder (no I/O, Qt, model or threads).

``observe_update(EngineUpdate) -> list[episode]`` copies a safe update then
processes it. ``submit(snapshot_update(update))`` consumes ownership of an
already detached message, for the recorder worker. ``close()`` seals the last
episode and returns its batch. Batches contain canonical decision ``records``
and episode-level quality metadata even when there are no trainable decisions.
A verified settlement publishes immediately. A later conflict publishes an
upsert with the same episode ID, downgraded to quarantine; duplicates emit nothing.

The synchronous API is lossless with respect to its returned batches: offline
callers must persist every returned batch themselves. It has no legacy parser,
network, filesystem, legal-action encoder or model adapter. It is single-owner,
not a thread-safe shared object. Consumers must not mutate returned data while
persisting it; the builder relinquishes references to sealed episodes.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from guance.contracts import EngineUpdate

from .schema import (
    SCHEMA_VERSION, SchemaError, build_decision_record, identifier,
    snapshot_update, stable_id,
)

INVALIDATION_REASONS = frozenset({
    "capture_error", "state_error", "reconnect", "queue_overflow", "writer_error",
    "invalid_update", "external_loss", "producer_contention",
})


class TrainingDataCollector:
    """Link exactly one contiguous confirmed action to its immutable pre-state.

    ``source_id`` is a stable logical input ID, NOT a path. An offline importer
    should use the source-file SHA256; identical inputs/IDs produce identical
    episode/record IDs. A live recorder supplies a fresh run ID by default.
    ``source`` is explicitly real/legacy/synthetic; they never share identities.
    """

    def __init__(
        self, *, source: str = "real", source_id: str = "desktop",
        rule_profile: str | None = None, rule_revision: str | None = None,
        max_decisions: int = 256,
    ) -> None:
        if source not in ("real", "legacy", "synthetic"):
            raise ValueError("unsupported_source")
        self.source = source
        self.source_id = identifier(source_id)
        self.rule_profile = None if rule_profile is None else identifier(rule_profile)
        self.rule_revision = None if rule_revision is None else identifier(rule_revision)
        if type(max_decisions) is not int or max_decisions < 1:
            raise ValueError("invalid_max_decisions")
        self._max_decisions = max_decisions
        self._current: dict[str, Any] | None = None
        self._closed = False
        self._sealed_keys: set[tuple[str, str]] = set()
        self._counts = {"episodes": 0, "decisions": 0, "provenance_only_actions": 0}

    def observe_update(self, update: EngineUpdate) -> list[dict[str, Any]]:
        try:
            message = snapshot_update(update)
        except (SchemaError, TypeError, ValueError):
            return self.invalidate_current("invalid_update")
        return self._updates() if message is None else self.submit(message)

    def observe_advice(self, result: Any) -> None:
        """Deliberate no-op. Advice/candidate/score data never enter this schema."""

    def submit(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        """Consume a validated, detached snapshot message, without copying it.

        Use ``observe_update`` for untrusted/direct inputs. This lower-level
        method is for the recorder and accepts only ``snapshot_update`` output.
        """
        if self._closed:
            return []
        table = message["table"]
        key = (table["session_id"], table["game_id"])
        reason = message["message"]
        current = self._current
        if reason == "deal" and (current is None or key != current["key"]):
            completed = self._seal()
            self._current = {
                "key": key, "table": table, "sequence": message["source_sequence"],
                "records": [], "reasons": [], "outcome": None,
                "episode_id": stable_id(self.source, self.source_id, *key),
                "provenance_only_actions": 0,
            }
            if table["history"] or table["phase"] != "waiting_start" or table["state_version"] != 1:
                self._invalidate("invalid_deal_boundary")
            if key in self._sealed_keys:
                self._invalidate("reused_game_identity")
            return completed
        if current is None:
            # Starting in the middle is never silently accepted as a fresh deal.
            self._current = {
                "key": key, "table": table, "sequence": message["source_sequence"],
                "records": [], "reasons": ["missing_deal"], "outcome": None,
                "episode_id": stable_id(self.source, self.source_id, *key),
                "provenance_only_actions": 0,
            }
            return self._updates()
        if key != current["key"]:
            self._invalidate("game_boundary_without_deal")
            return self._updates()
        if current["reasons"]:
            return self._updates()
        previous = current["table"]
        old_sequence, sequence = current["sequence"], message["source_sequence"]
        # EngineUpdate is not emitted for every raw frame. Forward raw-frame
        # gaps and several updates from one aggregate are both legitimate.
        if old_sequence is not None and sequence is not None and sequence < old_sequence:
            self._invalidate("source_sequence_regression")
            return self._updates()
        if _same_state(previous, table):
            current["sequence"] = sequence
            return self._updates()
        if reason == "deal":
            self._invalidate("deal_without_new_game")
            return self._updates()
        if current["outcome"] is not None:
            self._invalidate("settlement_conflict")
            return self._updates()
        delta = table["state_version"] - previous["state_version"]
        if delta != 1:
            self._invalidate("state_version_gap" if delta > 1 else "state_version_conflict")
            return self._updates()
        before, after = previous["history"], table["history"]
        if after[:len(before)] != before or len(after) < len(before):
            self._invalidate("history_conflict")
            return self._updates()
        if len(after) > len(before) + 1:
            self._invalidate("history_gap")
            return self._updates()
        if previous["level"] != table["level"] or (
            previous["my_seat"] >= 0 and previous["my_seat"] != table["my_seat"]
        ):
            self._invalidate("identity_conflict")
            return self._updates()
        if len(after) == len(before) + 1:
            if reason != "action" or not _action_transition(previous, table, after[-1]):
                self._invalidate("ambiguous_action_link")
                return self._updates()
            action = after[-1]
            if (previous["seat_confirmed"] and previous["my_seat"] >= 0
                    and previous["current_seat"] == previous["my_seat"] == action["player"]):
                if len(current["records"]) >= self._max_decisions:
                    self._invalidate("decision_buffer_limit")
                    return self._updates()
                current["records"].append(build_decision_record(
                    source=self.source, source_id=self.source_id,
                    episode_id=current["episode_id"], pre_state=previous, action=action,
                    post_state=table, source_sequence=sequence,
                    rule_profile=self.rule_profile, rule_revision=self.rule_revision,
                ))
                self._counts["decisions"] += 1
            else:
                current["provenance_only_actions"] += 1
                self._counts["provenance_only_actions"] += 1
        elif reason == "settlement":
            ranks = table.get("finish_ranks_by_seat", [])
            if (previous["phase"] != "ended" or table["phase"] != "settled"
                    or not previous["seat_confirmed"] or sorted(ranks) != [1, 2, 3, 4]
                    or not _same_gameplay(previous, table)):
                self._invalidate("invalid_settlement")
                return self._updates()
            current["outcome"] = {"phase": "settled", "finish_ranks_by_seat": list(ranks)}
        elif reason == "start":
            if (previous["phase"] != "waiting_start" or table["phase"] != "active"
                    or previous["hand"] != table["hand"] or previous["remaining"] != table["remaining"]):
                self._invalidate("invalid_start")
                return self._updates()
        elif reason in ("seat-evidence", "manual-seat-confirmation"):
            if not _same_gameplay(previous, table):
                self._invalidate("seat_evidence_changed_gameplay")
                return self._updates()
        else:
            self._invalidate("unrecognized_state_transition")
            return self._updates()
        current["table"] = table
        current["sequence"] = sequence
        return self._updates()

    def invalidate_current(self, reason: str = "external_loss") -> list[dict[str, Any]]:
        """Return a quarantine upsert if an already-published episode is revoked."""
        self._invalidate(reason if reason in INVALIDATION_REASONS else "external_loss")
        return self._updates()

    def _invalidate(self, reason: str) -> None:
        if self._current is not None and reason not in self._current["reasons"]:
            self._current["reasons"].append(reason)
            # A disputed episode must not retain an authoritative outcome.
            self._current["outcome"] = None

    def boundary(self, reason: str = "capture_closed") -> list[dict[str, Any]]:
        """Seal and forget current game; does not authorize a new non-Deal state.

        A controller reconnect/error should use invalidate_current, not boundary.
        ``close`` is the ordinary end-of-run boundary.
        """
        if reason != "capture_closed":
            self.invalidate_current(reason)
        return self._seal()

    def close(self) -> list[dict[str, Any]]:
        if self._closed:
            return []
        self._closed = True
        return self._seal()

    def stats(self) -> dict[str, Any]:
        current = self._current
        return {
            **self._counts, "closed": self._closed,
            "pending_records": len(current["records"]) if current and "last_digest" not in current else 0,
            "active_invalidated": bool(current and current["reasons"]),
            "active_episode_id": current["episode_id"] if current else None,
        }

    def _seal(self) -> list[dict[str, Any]]:
        if self._current is None:
            return []
        result = self._publish()
        self._sealed_keys.add(self._current["key"])
        self._current = None
        self._counts["episodes"] += 1
        return result

    def _updates(self) -> list[dict[str, Any]]:
        current = self._current
        if current and (current["outcome"] is not None or "last_digest" in current):
            return self._publish()
        return []

    def _publish(self) -> list[dict[str, Any]]:
        current = self._current
        if current is None:
            return []
        reasons = list(current["reasons"])
        status = "quarantine" if reasons else "accepted_candidate" if current["outcome"] else "partial"
        if not reasons and current["outcome"] is None:
            reasons = ["ended_without_settlement" if current["table"]["phase"] == "ended" else "missing_settlement"]
        lost = any(x in reasons for x in (
            "queue_overflow", "writer_error", "external_loss", "producer_contention", "decision_buffer_limit",
        ))
        records = deepcopy(current["records"])
        for record in records:
            record["status"] = status
            record["quality"] = {"status": status, "reasons": list(reasons), "lost": lost}
            record["outcome"] = deepcopy(current["outcome"])
        episode = {
            "record_type": "episode", "schema_version": SCHEMA_VERSION,
            "episode_id": current["episode_id"], "source": self.source, "source_id": self.source_id,
            "session_id": current["key"][0], "game_id": current["key"][1],
            "status": status, "reasons": reasons, "lost": lost,
            "provenance_only_actions": current["provenance_only_actions"], "records": records,
        }
        digest = stable_id(episode)
        if digest == current.get("last_digest"):
            return []
        current["last_digest"] = digest
        return [episode]


def _same_state(left: dict, right: dict) -> bool:
    return {k: v for k, v in left.items() if k != "captured_at"} == {
        k: v for k, v in right.items() if k != "captured_at"
    }


def _same_gameplay(left: dict, right: dict) -> bool:
    return all(left.get(key) == right.get(key) for key in (
        "level", "hand", "remaining", "history", "first_seat", "current_seat", "free_lead", "trick", "lead_action",
    ))


def _action_transition(pre: dict, post: dict, action: dict) -> bool:
    if pre["phase"] != "active" or post["phase"] not in ("active", "ended"):
        return False
    if pre["current_seat"] != action["player"]:
        return False
    if post["current_seat"] != (-1 if action["next_player"] is None else action["next_player"]):
        return False
    remaining = list(pre["remaining"])
    remaining[action["player"]] -= len(action["cards"])
    if remaining != post["remaining"] or pre["first_seat"] != post["first_seat"]:
        return False
    hand = set(pre["hand"])
    if post["my_seat"] == action["player"]:
        if not set(action["cards"]).issubset(hand):
            return False
        hand.difference_update(action["cards"])
    return sorted(hand) == post["hand"]
