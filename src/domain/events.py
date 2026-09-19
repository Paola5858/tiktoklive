"""Modelo de evento interno do domínio.

O domínio não conhece TikTok, Roblox, FastAPI nem nenhuma infraestrutura —
ele só entende o conceito de "evento" e as invariantes que um evento
precisa respeitar pra ser considerado válido. Ver `context/EVENT_SCHEMA.md`
pra descrição completa do schema.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from src.domain.errors import InvalidEventError


class EventType(str, Enum):
    """Tipos de evento suportados pelo domínio."""

    COMMENT = "COMMENT"
    GIFT = "GIFT"
    FOLLOW = "FOLLOW"
    SHARE = "SHARE"
    LIKE = "LIKE"
    SYSTEM = "SYSTEM"
    MANUAL = "MANUAL"
    CUSTOM = "CUSTOM"


class EventStatus(str, Enum):
    """Ciclo de vida de um evento, do recebimento ao resultado final."""

    RECEIVED = "RECEIVED"
    NORMALIZED = "NORMALIZED"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    DROPPED = "DROPPED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class EventUser:
    """Identidade do usuário associada a um evento.

    `external_id` é o identificador na origem (ex: id do usuário no
    TikTok) e pode não existir pra toda fonte — por isso é opcional.
    """

    display_name: str
    external_id: str | None = None

    def __post_init__(self) -> None:
        if not self.display_name or not self.display_name.strip():
            raise InvalidEventError("EventUser.display_name não pode ser vazio")


@dataclass(frozen=True, slots=True)
class Event:
    """Evento interno padronizado — o contrato entre NORMALIZER e EVENT_ENGINE."""

    event_type: EventType
    source: str
    user: EventUser
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    source_event_id: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: EventStatus = EventStatus.RECEIVED

    def __post_init__(self) -> None:
        if not self.source or not self.source.strip():
            raise InvalidEventError("Event.source não pode ser vazio")
        if not isinstance(self.payload, dict):
            raise InvalidEventError(
                f"Event.payload precisa ser um dict, recebeu {type(self.payload).__name__}"
            )
        if self.timestamp.tzinfo is None:
            raise InvalidEventError("Event.timestamp precisa ser timezone-aware")

    def deduplication_key(self) -> str:
        """Chave usada pra identificar eventos duplicados.

        Regra atual (baseline, a validar com dados reais de live — ver
        `context/TEST_PLAN.md`): mesma origem + mesmo tipo + mesmo
        usuário + mesmo conteúdo relevante do payload contam como o
        mesmo evento. Pra GIFT e COMMENT o conteúdo relevante é
        explícito; pros demais tipos, cai pro payload inteiro
        serializado — intencionalmente conservador até termos dados
        reais pra calibrar.
        """
        user_key = self.user.external_id or self.user.display_name
        if self.event_type == EventType.GIFT:
            content_key = str(self.payload.get("gift_name", ""))
        elif self.event_type == EventType.COMMENT:
            content_key = str(self.payload.get("text", ""))
        else:
            content_key = repr(sorted(self.payload.items()))
        return f"{self.source}:{self.event_type.value}:{user_key}:{content_key}"
