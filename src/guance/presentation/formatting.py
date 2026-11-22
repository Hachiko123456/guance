"""Presentation-only formatting shared by the main window and compact window."""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from guance.domain.cards import card_parts

SUITS = {1: "♠", 2: "♥", 3: "♣", 4: "♦"}
RANKS = {1: "A", 11: "J", 12: "Q", 13: "K"}
KIND_NAMES = {
    "pass": "过牌", "single": "单张", "pair": "对子", "triple": "三张",
    "trips": "三张", "full_house": "三带二", "straight": "顺子",
    "straight_flush": "同花顺", "bomb": "炸弹", "steel_plate": "钢板",
    "consecutive_pairs": "连对", "joker_bomb": "四王炸弹",
    "full": "三带二", "serial_pair": "连对", "serial_triple": "钢板",
    "sflush": "同花顺", "rocket": "天王炸",
}
_SEQUENCE_SHAPES = {
    "4": (5, 1), "straight": (5, 1),
    "6": (3, 2), "serial_pair": (3, 2), "consecutive_pairs": (3, 2),
    "7": (2, 3), "serial_triple": (2, 3), "steel_plate": (2, 3),
    "9": (5, 1), "sflush": (5, 1), "straight_flush": (5, 1),
}
_PRINTED_SEQUENCE = (1, *range(2, 14), 1)
_SECRET = re.compile(
    r"(?i)(authorization|cookie|token|password|secret|private_key|openid|session_key)"
    r"(\s*[:=]\s*)(?:Bearer\s+)?[^\s;,]+"
)


def safe_text(value: Any, limit: int = 2000) -> str:
    """Defence in depth for diagnostics, not a substitute for upstream redaction."""
    text = str(value or "")
    if "PRIVATE KEY" in text:
        return "[已隐藏私钥内容]"
    return _SECRET.sub(r"\1\2[已隐藏]", text[:limit])


def display_card(value: Any, level: int = 0) -> dict[str, Any]:
    """Convert a contract card code (or explicit display fixture) into QML data."""
    if isinstance(value, dict):
        rank = str(value.get("rank", "?"))
        suit = str(value.get("suit", ""))
        return {
            "rank": rank, "suit": suit, "red": bool(value.get("red", suit in "♥♦" and bool(suit))),
            "wild": bool(value.get("wild", False)), "label": f"{suit}{rank}",
            "joker": bool(value.get("joker", False)),
        }
    try:
        suit, rank, _ = card_parts(value)
    except (ValueError, TypeError):
        return {"rank": "?", "suit": "", "red": False, "wild": False,
                "label": "无法识别的牌", "joker": False}
    joker = suit == 0
    rank_text = ("大王" if rank == 15 else "小王") if joker else RANKS.get(rank, str(rank))
    symbol = "" if joker else SUITS[suit]
    return {"rank": rank_text, "suit": symbol, "red": rank == 15 if joker else suit in (2, 4),
            "wild": suit == 2 and rank == level, "label": f"{symbol}{rank_text}", "joker": joker}


def seat_name(seat: int, my_seat: int) -> str:
    if seat < 0 or my_seat < 0:
        return "等待同步"
    return {0: "自己", 1: "左家", 2: "对家", 3: "右家"}[(seat - my_seat) % 4]


def kind_name(kind: str) -> str:
    return KIND_NAMES.get(kind, kind if any("\u4e00" <= c <= "\u9fff" for c in kind) else "出牌")


def hand_display_order(values: list[int] | tuple[int, ...], level: int) -> list[int]:
    """Return display-only cards in Dagandan strength order (high to low)."""
    def key(code: int) -> tuple[int, int, int]:
        try:
            suit, rank, copy = card_parts(code)
        except (ValueError, TypeError):
            return (-1, 0, 0)
        if suit == 0:
            strength = 100 if rank == 15 else 99
        elif rank == level and 1 <= level <= 13:
            strength = 98
        elif rank == 1:
            strength = 97
        else:
            strength = 80 + rank
        return strength, suit, copy
    return sorted(values, key=key, reverse=True)


def public_play_display_order(values: list[int] | tuple[int, ...], level: int,
                              kind: str | int | None = None) -> list[int]:
    """Order displayed plays by their combination, preserving each physical card."""
    ordered = list(reversed(hand_display_order(values, level)))
    shape = _SEQUENCE_SHAPES.get(str(kind).lower())
    full_house = str(kind).lower() in {"5", "full", "full_house", "triple+2"}
    if not shape and not full_house:
        return ordered
    try:
        faces = [(code, *card_parts(code)[:2]) for code in ordered]
    except (TypeError, ValueError):
        return ordered
    if full_house:
        counts = Counter(rank for _, _, rank in faces)
        if sorted(counts.values()) == [2, 3]:
            return sorted(ordered, key=lambda code: -counts[card_parts(code)[1]])
        return ordered
    length, group = shape
    if len(faces) != length * group:
        return ordered
    wilds = [code for code, suit, rank in faces if suit == 2 and rank == level]
    if result := _sequence_order(faces, (), length, group):
        return result
    if wilds:
        return _sequence_order([face for face in faces if face[0] not in wilds],
                               wilds, length, group) or ordered
    return ordered


def _sequence_order(faces, wilds, length: int, group: int) -> list[int] | None:
    cards_by_rank: dict[int, list[int]] = {}
    for code, _, rank in faces:
        cards_by_rank.setdefault(rank, []).append(code)
    if any(len(cards) > group for cards in cards_by_rank.values()):
        return None
    windows = [_PRINTED_SEQUENCE[i:i + length]
               for i in range(len(_PRINTED_SEQUENCE) - length + 1)]
    windows = [window for window in windows if cards_by_rank.keys() <= set(window)]
    if len(windows) != 1:
        return None
    ordered = []
    substitutions = iter(wilds)
    for rank in windows[0]:
        group_cards = cards_by_rank.get(rank, ())
        ordered.extend(group_cards)
        ordered.extend(next(substitutions) for _ in range(group - len(group_cards)))
    return ordered
