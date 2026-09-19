"""Modelo de evento interno do domínio.

O domínio não conhece TikTok, Roblox, FastAPI nem nenhuma infraestrutura —
ele só entende o conceito de "evento" e as invariantes que um evento
precisa respeitar pra ser considerado válido. Ver `context/EVENT_SCHEMA.md`
pra descrição completa do schema.
"""

from __future__ import annotations

import json
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

    RECEIVED = "received"
    NORMALIZED = "normalized"
    ACCEPTED = "accepted"
    QUEUED = "queued"
    PROCESSING = "processing"
    PROCESSED = "processed"
    AGGREGATED = "aggregated"
    DROPPED = "dropped"
    FAILED = "failed"


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
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: EventStatus = EventStatus.RECEIVED

    def __post_init__(self) -> None:
        if not isinstance(self.event_type, EventType):
            raise InvalidEventError("Event.event_type precisa ser um EventType")
        if not isinstance(self.user, EventUser):
            raise InvalidEventError("Event.user precisa ser um EventUser")
        if not self.source or not self.source.strip():
            raise InvalidEventError("Event.source não pode ser vazio")
        if not isinstance(self.payload, dict):
            raise InvalidEventError(
                f"Event.payload precisa ser um dict, recebeu {type(self.payload).__name__}"
            )
        if not isinstance(self.timestamp, datetime) or self.timestamp.utcoffset() is None:
            raise InvalidEventError("Event.timestamp precisa ser timezone-aware")
        if not isinstance(self.received_at, datetime) or self.received_at.utcoffset() is None:
            raise InvalidEventError("Event.received_at precisa ser timezone-aware")

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
            content_key = self.payload.get("gift_id", self.payload.get("gift_name", ""))
        elif self.event_type == EventType.COMMENT:
            content_key = str(self.payload.get("text", ""))
        else:
            content_key = self.payload
        return json.dumps(
            {
                "source": self.source,
                "event_type": self.event_type.value,
                "user": user_key,
                "content": content_key,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
