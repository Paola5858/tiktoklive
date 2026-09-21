"""Modelos declarativos da camada de regras de interação.

Nenhum valor vindo da live é executado como código. As regras são dados
validados e as ações passam por uma allowlist fechada antes de virar GameEvent.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping

from src.domain.events import Event, EventType
from src.domain.priorities import Priority, resolve_priority


class ActionType(str, Enum):
    SPAWN_AVATAR = "SPAWN_AVATAR"
    PLAY_EFFECT = "PLAY_EFFECT"
    SHOW_MESSAGE = "SHOW_MESSAGE"
    UPDATE_COUNTER = "UPDATE_COUNTER"


IMPLEMENTED_ACTION_TYPES = frozenset({ActionType.SPAWN_AVATAR})


def _bounded_string(value: Any, field_name: str, maximum: int = 128) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} deve ser uma string não vazia")
    value = value.strip()
    if len(value) > maximum:
        raise ValueError(f"{field_name} excede {maximum} caracteres")
    return value


@dataclass(frozen=True, slots=True)
class RuleMatch:
    event_type: EventType
    gift_id: int | None = None
    gift_name: str | None = None
    keyword: str | None = None
    case_sensitive: bool = False
    min_quantity: int | None = None
    user_external_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.event_type, EventType):
            raise ValueError("match.event_type inválido")
        if self.gift_id is not None and (not isinstance(self.gift_id, int) or self.gift_id <= 0):
            raise ValueError("match.gift_id deve ser inteiro positivo")
        for name in ("gift_name", "keyword", "user_external_id"):
            value = getattr(self, name)
            if value is not None:
                _bounded_string(value, f"match.{name}", 256)
        if self.min_quantity is not None and (not isinstance(self.min_quantity, int) or not 1 <= self.min_quantity <= 1_000_000):
            raise ValueError("match.min_quantity deve estar entre 1 e 1000000")
        if self.event_type != EventType.GIFT and any(v is not None for v in (self.gift_id, self.gift_name, self.min_quantity)):
            raise ValueError("filtros de gift só podem ser usados em regras GIFT")
        if self.keyword is not None and self.event_type != EventType.COMMENT:
            raise ValueError("match.keyword só pode ser usado em regras COMMENT")


@dataclass(frozen=True, slots=True)
class CooldownSpec:
    scope: str = "rule"
    seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.scope not in {"rule", "gift", "user", "global"}:
            raise ValueError("cooldown.scope inválido")
        if not isinstance(self.seconds, (int, float)) or not 0 <= self.seconds <= 86_400:
            raise ValueError("cooldown.seconds deve estar entre 0 e 86400")


@dataclass(frozen=True, slots=True)
class AggregationSpec:
    window_seconds: float = 0.0
    threshold: int = 0
    key: str = "event_type"

    def __post_init__(self) -> None:
        if not 0 <= self.window_seconds <= 300:
            raise ValueError("aggregation.window_seconds deve estar entre 0 e 300")
        if not 0 <= self.threshold <= 10_000:
            raise ValueError("aggregation.threshold deve estar entre 0 e 10000")
        if self.key not in {"event_type", "gift_id", "keyword", "user"}:
            raise ValueError("aggregation.key inválida")
        if self.threshold and not self.window_seconds:
            raise ValueError("aggregation com threshold exige window_seconds")


@dataclass(frozen=True, slots=True)
class ActionDefinition:
    action_type: ActionType
    params: dict[str, Any] = field(default_factory=dict)
    action_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __post_init__(self) -> None:
        if self.action_type not in IMPLEMENTED_ACTION_TYPES:
            raise ValueError(f"action_type não implementado: {self.action_type}")
        if not isinstance(self.params, dict):
            raise ValueError("action.params deve ser dict")
        if len(self.params) > 32:
            raise ValueError("action.params excede o limite de campos")
        if self.action_type == ActionType.SPAWN_AVATAR:
            duration = self.params.get("duration_seconds", 60)
            if not isinstance(duration, (int, float)) or not 1 <= duration <= 86_400:
                raise ValueError("duration_seconds inválido")
            effect = self.params.get("effect")
            if effect is not None and effect not in {"HEARTS", "GOLD_AURA", "BLUE_GLOW"}:
                raise ValueError(
                    f"effect '{effect}' não está na allowlist do runtime Roblox"
                )
        if self.action_type == ActionType.PLAY_EFFECT:
            effect = self.params.get("effect")
            if effect not in {"HEARTS", "GOLD_AURA", "BLUE_GLOW"}:
                raise ValueError("effect não está na allowlist do runtime Roblox")


@dataclass(frozen=True, slots=True)
class InteractionRule:
    rule_id: str
    enabled: bool
    match: RuleMatch
    actions: tuple[ActionDefinition, ...]
    priority: Priority | None = None
    cooldown: CooldownSpec = field(default_factory=CooldownSpec)
    aggregation: AggregationSpec = field(default_factory=AggregationSpec)
    stop_processing: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _bounded_string(self.rule_id, "rule_id", 128)
        if not self.actions or len(self.actions) > 8:
            raise ValueError("uma regra deve ter entre 1 e 8 ações")
        if self.priority is not None and not isinstance(self.priority, Priority):
            raise ValueError("rule.priority inválida")
        if len(self.metadata) > 16:
            raise ValueError("rule.metadata excede o limite de campos")

    @property
    def resolved_priority(self) -> Priority:
        return self.priority or resolve_priority(self.match.event_type)


@dataclass(frozen=True, slots=True)
class ActionResult:
    action_id: str
    action_type: ActionType
    source_event_id: str
    rule_id: str
    priority: Priority
    created_at: datetime
    expires_at: datetime | None
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class GameEvent:
    """Comando gameplay-ready que pode atravessar o Roblox Bridge."""

    event_id: str
    event_type: str
    priority: Priority
    timestamp: datetime
    payload: dict[str, Any]
    source_event_id: str
    expires_at: datetime | None = None
    schema_version: str = "1.0"

    def to_dict(self) -> dict[str, Any]:
        def iso(value: datetime | None) -> str | None:
            return value.astimezone(timezone.utc).isoformat() if value else None

        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "priority": int(self.priority),
            "timestamp": iso(self.timestamp),
            "payload": self.payload,
            "source_event_id": self.source_event_id,
            "expires_at": iso(self.expires_at),
            "idempotency_key": f"{self.source_event_id}:{self.event_id}",
        }


def rule_from_dict(raw: Mapping[str, Any]) -> InteractionRule:
    """Converte configuração estruturada em regra validada."""
    if not isinstance(raw, Mapping):
        raise ValueError("regra deve ser um objeto")
    event_type = EventType(str(raw.get("event_type", "")))
    match_raw = raw.get("match") or {}
    match = RuleMatch(
        event_type=event_type,
        gift_id=match_raw.get("gift_id"),
        gift_name=match_raw.get("gift_name"),
        keyword=match_raw.get("keyword"),
        case_sensitive=bool(match_raw.get("case_sensitive", False)),
        min_quantity=match_raw.get("min_quantity"),
        user_external_id=match_raw.get("user_external_id"),
    )
    actions = tuple(
        ActionDefinition(ActionType(str(item.get("type", ""))), dict(item.get("params") or {}))
        for item in raw.get("actions", [])
    )
    priority_raw = raw.get("priority")
    priority = Priority[str(priority_raw)] if isinstance(priority_raw, str) else (Priority(priority_raw) if priority_raw is not None else None)
    cooldown_raw = raw.get("cooldown") or {}
    aggregation_raw = raw.get("aggregation") or {}
    return InteractionRule(
        rule_id=_bounded_string(raw.get("id"), "id"),
        enabled=bool(raw.get("enabled", True)),
        match=match,
        actions=actions,
        priority=priority,
        cooldown=CooldownSpec(cooldown_raw.get("scope", "rule"), cooldown_raw.get("seconds", 0)),
        aggregation=AggregationSpec(
            aggregation_raw.get("window_seconds", 0),
            aggregation_raw.get("threshold", 0),
            aggregation_raw.get("key", "event_type"),
        ),
        stop_processing=bool(raw.get("stop_processing", False)),
        metadata=dict(raw.get("metadata") or {}),
    )
