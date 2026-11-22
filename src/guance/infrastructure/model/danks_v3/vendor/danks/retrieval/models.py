from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class CardGroup:
    """One component in a hand partition."""

    kind: str
    cards: tuple[str, ...]
    rank: str | None = None
    strength: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def size(self) -> int:
        return len(self.cards)


def effective_group_cost(group: CardGroup) -> int:
    """Return one group's contribution to Retrieval effective hand count."""

    if group.kind in {"Bomb", "StraightFlush", "FourKings"}:
        return -1
    if group.kind == "Single" and (group.rank == "RJ" or group.cards == ("RJ",)):
        return -1
    # Card strings use T for 10, so Straight/rank A is exactly 10JQKA;
    # the A2345 straight is ranked as 5 and remains an ordinary group.
    if group.kind == "Straight" and group.rank == "A":
        return -1
    return 1


@dataclass(frozen=True, slots=True)
class Partition:
    """A complete non-overlapping cover of one hand."""

    groups: tuple[CardGroup, ...]
    mode: str = "heuristic"

    @property
    def hand_count(self) -> int:
        return len(self.groups)

    @property
    def effective_hand_count(self) -> int:
        return sum(effective_group_cost(group) for group in self.groups)

    @property
    def cards(self) -> tuple[str, ...]:
        out: list[str] = []
        for group in self.groups:
            out.extend(group.cards)
        return tuple(out)


@dataclass(frozen=True, slots=True)
class NativePartitionCovers:
    """Native cover ids whose Partition objects are materialized on demand."""

    groups: tuple[CardGroup, ...]
    covers: tuple[tuple[int, ...], ...]
    mode: str

    def materialize(self) -> list[Partition]:
        return [self.materialize_one(index) for index in range(len(self.covers))]

    def materialize_one(self, index: int) -> Partition:
        return Partition(
            tuple(self.groups[group_id] for group_id in self.covers[index]),
            self.mode,
        )


@dataclass(frozen=True, slots=True)
class ActionCandidate:
    index: int
    kind: str
    cards: tuple[str, ...]
    rank: str | None = None

    @property
    def size(self) -> int:
        return len(self.cards)


@dataclass(frozen=True, slots=True)
class ScoredAction:
    action: ActionCandidate
    score: float
    partition: Partition
    hand_count_score: float
    card_value_score: float
    retake_score: float
    details: dict[str, Any] = field(default_factory=dict)


# Vendored for Guance danks_v3 from DanKS-Train versions/v3/DanKS/retrieval/models.py
# reference commit c618f537c7a83c77b76c3082985e42efff67ec02; original sha256 fc7af525b109cc8b93d7ae7b4806875c4a3ee1e64e59e100431bec4186e7e22d;
# only absolute import roots were rewritten (DanKS./guandan. -> vendored package roots).
