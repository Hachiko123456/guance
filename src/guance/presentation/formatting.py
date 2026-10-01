"""Presentation-only formatting shared by the main window and compact window."""
from __future__ import annotations

import re
from typing import Any

from guance.domain.cards import card_parts

SUITS = {1: "♠", 2: "♥", 3: "♣", 4: "♦"}
RANKS = {1: "A", 11: "J", 12: "Q", 13: "K"}
KIND_NAMES = {
    "pass": "过牌", "single": "单张", "pair": "对子", "triple": "三张",
    "trips": "三张", "full_house": "三带二", "straight": "顺子",
    "straight_flush": "同花顺", "bomb": "炸弹", "steel_plate": "钢板",
    "consecutive_pairs": "连对", "joker_bomb": "四王炸弹",
}
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


def public_play_display_order(values: list[int] | tuple[int, ...], level: int) -> list[int]:
    """Return a new public-play list, low to high, without changing source cards."""
    return list(reversed(hand_display_order(values, level)))
