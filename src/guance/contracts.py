"""Versioned, Qt-free contracts shared by core, model, runtime and UI adapters."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Literal

SCHEMA_VERSION = 1

@dataclass(frozen=True, slots=True)
class SourceFrame:
    host: str
    payload: bytes
    received_at: float
    connection_id: str = ""
    sequence: int = 0

@dataclass(frozen=True, slots=True)
class PublicAction:
    index: int
    player: int
    kind: Literal["play", "pass"]
    cards: tuple[int, ...] = ()
    combo_type: int | None = None
    next_player: int | None = None
    trick: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "player": self.player, "kind": self.kind,
                "cards": list(self.cards), "combo_type": self.combo_type,
                "next_player": self.next_player, "trick": self.trick}

@dataclass(frozen=True, slots=True)
class GameObservation:
    session_id: str
    game_id: str
    state_version: int
    captured_at: float
    level: int
    my_seat: int
    first_seat: int
    current_seat: int
    hand: tuple[int, ...]
    remaining: tuple[int, int, int, int]
    history: tuple[PublicAction, ...] = ()
    lead_action: PublicAction | None = None
    free_lead: bool = False
    trick: int = 0

    @property
    def partner_seat(self) -> int:
        return (self.my_seat + 2) % 4

    @property
    def key(self) -> tuple[str, str, int]:
        return self.session_id, self.game_id, self.state_version

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "session_id": self.session_id,
                "game_id": self.game_id, "state_version": self.state_version,
                "captured_at": self.captured_at, "level": self.level,
                "my_seat": self.my_seat, "partner_seat": self.partner_seat,
                "first_seat": self.first_seat, "current_seat": self.current_seat,
                "hand": list(self.hand), "remaining": list(self.remaining),
                "history": [x.to_dict() for x in self.history],
                "lead_action": self.lead_action.to_dict() if self.lead_action else None,
                "free_lead": self.free_lead, "trick": self.trick}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GameObservation:
        def action(x: dict[str, Any]) -> PublicAction:
            return PublicAction(index=int(x["index"]), player=int(x["player"]),
                kind=x["kind"], cards=tuple(x.get("cards", ())),
                combo_type=x.get("combo_type"), next_player=x.get("next_player"),
                trick=int(x.get("trick", 0)))
        return cls(session_id=str(data["session_id"]), game_id=str(data["game_id"]),
            state_version=int(data["state_version"]), captured_at=float(data["captured_at"]),
            level=int(data["level"]), my_seat=int(data["my_seat"]),
            first_seat=int(data["first_seat"]), current_seat=int(data["current_seat"]),
            hand=tuple(data["hand"]), remaining=tuple(data["remaining"]),
            history=tuple(action(x) for x in data.get("history", ())),
            lead_action=action(data["lead_action"]) if data.get("lead_action") else None,
            free_lead=bool(data.get("free_lead")), trick=int(data.get("trick", 0)))

@dataclass(frozen=True, slots=True)
class AdviceCandidate:
    cards: tuple[int, ...]
    kind: str
    label: str
    score: float | None = None
    claim: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"cards": list(self.cards), "kind": self.kind, "label": self.label,
                "score": self.score, "claim": list(self.claim)}

@dataclass(frozen=True, slots=True)
class AdviceResult:
    session_id: str
    game_id: str
    state_version: int
    request_id: str
    candidates: tuple[AdviceCandidate, ...] = ()
    elapsed_ms: float = 0.0
    status: str = "ready"
    error_code: str = ""
    error_message: str = ""
    model_fingerprint: str = ""

    @property
    def key(self) -> tuple[str, str, int]:
        return self.session_id, self.game_id, self.state_version

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "session_id": self.session_id,
            "game_id": self.game_id, "state_version": self.state_version,
            "request_id": self.request_id, "candidates": [c.to_dict() for c in self.candidates],
            "elapsed_ms": self.elapsed_ms, "status": self.status,
            "error_code": self.error_code, "error_message": self.error_message,
            "model_fingerprint": self.model_fingerprint}

@dataclass(frozen=True, slots=True)
class EngineUpdate:
    kind: str
    message: str = ""
    observation: GameObservation | None = None
    data: dict[str, Any] = field(default_factory=dict)
    error_code: str = ""
