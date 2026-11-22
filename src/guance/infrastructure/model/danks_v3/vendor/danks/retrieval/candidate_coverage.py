from __future__ import annotations

from collections.abc import Sequence

from .models import ActionCandidate, ScoredAction
from .rules import is_bomb_kind, normalize_kind


def tactical_coverage_indices(
    actions: Sequence[ActionCandidate],
    preference_order: Sequence[int],
    *,
    top_k: int,
) -> list[int]:
    """Reserve response categories without changing any candidate score.

    Coverage is active only when PASS is legal, which identifies a response
    decision.  The best PASS, normal response, and bomb response in the supplied
    preference order are retained when capacity permits.  Lead decisions keep
    their preference prefix unchanged.
    """

    preference = [int(index) for index in preference_order]
    if len(set(preference)) != len(preference):
        raise ValueError("preference_order must not contain duplicates")
    if any(index < 0 or index >= len(actions) for index in preference):
        raise IndexError("preference_order index outside actions")
    target = min(max(0, int(top_k)), len(preference))
    if target <= 0:
        return []

    def kind(index: int) -> str:
        return normalize_kind(actions[index].kind)

    if not any(kind(index) == "PASS" for index in preference):
        return preference[:target]

    categories = (
        lambda value: value == "PASS",
        lambda value: value != "PASS" and not is_bomb_kind(value),
        is_bomb_kind,
    )
    required: list[int] = []
    for matches in categories:
        anchor = next(
            (index for index in preference if matches(kind(index))),
            None,
        )
        if anchor is not None and len(required) < target:
            required.append(anchor)

    selected = set(required)
    for index in preference:
        if len(selected) >= target:
            break
        selected.add(index)
    return [index for index in preference if index in selected]


def tactical_topk(rows: Sequence[ScoredAction], top_k: int) -> list[ScoredAction]:
    """Return the score-ordered TopK with response-category coverage."""

    preference = sorted(
        range(len(rows)),
        key=lambda index: rows[index].score,
        reverse=True,
    )
    selected = tactical_coverage_indices(
        [row.action for row in rows], preference, top_k=top_k,
    )
    return [rows[index] for index in selected]


# Vendored for Guance danks_v3 from DanKS-Train versions/v3/DanKS/retrieval/candidate_coverage.py
# reference commit c618f537c7a83c77b76c3082985e42efff67ec02; original sha256 6f27dfb41d85c0cd4d7be3a553f628daf782e0672522a3cc3134a99ce669be65;
# only absolute import roots were rewritten (DanKS./guandan. -> vendored package roots).
