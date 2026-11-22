from __future__ import annotations

from .context import RetrievalContext
from .models import ActionCandidate, CardGroup
from .partitioner import FullSearchPartitioner
from .rules import can_follow_action, group_to_action, normalize_kind


class ActionGenerator:
    """Generate legal GuanDan action candidates from full-search groups.

    The generator enumerates groups from the full-search universe and filters
    them with the canonical comparison rules. It does not force a fixed hand
    partition before constructing candidate actions.
    """

    def __init__(self, partitioner: FullSearchPartitioner | None = None) -> None:
        self.partitioner = partitioner or FullSearchPartitioner()

    def generate(self, hand_cards: list[str], ctx: RetrievalContext, include_pass: bool = True) -> list[ActionCandidate]:
        groups = self.partitioner._generate_groups(hand_cards, ctx)
        actions: list[ActionCandidate] = []
        if include_pass and ctx.current_kind != "Lead":
            actions.append(ActionCandidate(index=0, kind="PASS", cards=(), rank="PASS"))
        target = self._target_action(ctx)
        seen = set()
        for group in groups:
            action = group_to_action(group, len(actions))
            key = (action.kind, action.rank, action.cards)
            if key in seen:
                continue
            if target is not None and not can_follow_action(action, target, ctx.cur_rank):
                continue
            seen.add(key)
            actions.append(action)
        for idx, action in enumerate(actions):
            actions[idx] = ActionCandidate(index=idx, kind=action.kind, cards=action.cards, rank=action.rank)
        return actions

    def _target_action(self, ctx: RetrievalContext) -> ActionCandidate | None:
        kind = normalize_kind(ctx.current_kind)
        if kind == "Lead":
            return None
        size = ctx.current_size
        if size <= 0:
            size = {
                "Single": 1,
                "Pair": 2,
                "Triple": 3,
                "Straight": 5,
                "TriplePlus": 5,
                "StraightPair": 6,
                "StraightTriple": 6,
                "Bomb": 4,
                "StraightFlush": 5,
                "FourKings": 4,
            }.get(kind, 0)
        cards = tuple(f"__{i}" for i in range(size))
        return ActionCandidate(index=-1, kind=kind, cards=cards, rank=ctx.current_rank)


# Vendored for Guance danks_v3 from DanKS-Train versions/v3/DanKS/retrieval/action_generator.py
# reference commit c618f537c7a83c77b76c3082985e42efff67ec02; original sha256 e4bcd95145b18995790b0b1e7d8a7573095dfdc7ff6d50d7feb6bfbb1eb5dd82;
# only absolute import roots were rewritten (DanKS./guandan. -> vendored package roots).
