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
from typing import TYPE_CHECKING, Any

from src.domain.errors import InvalidEventError

if TYPE_CHECKING:
    from src.domain.priorities import Priority


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
    DEDUPLICATED = "deduplicated"  # identificado como duplicata, descartado
    CLASSIFIED = "classified"  # prioridade atribuída, pronto para enfileirar
    QUEUED = "queued"
    PROCESSING = "processing"
    PROCESSED = "processed"
    AGGREGATED = "aggregated"
    DROPPED = "dropped"
    FAILED = "failed"
    REJECTED = "rejected"  # falhou na validação de entrada


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


@dataclass(frozen=True, slots=True)
class AggregatedEvent:
    """Múltiplos eventos do mesmo tipo colapsados em um único registro.

    Produzido pelo EventAggregator quando eventos P4 ou floods de comentário
    repetitivo são agrupados numa janela temporal.

    Invariantes:
    - count >= 2 (um único evento não é um aggregate)
    - event_type é o tipo dos eventos originais
    - representative_payload é o payload do primeiro evento da janela
    - priority é a prioridade dos eventos originais
    - source é a fonte dos eventos originais (devem ser da mesma fonte)

    O que NÃO é preservado:
    - Identidade individual de cada evento (event_id, user por evento)
    - Timestamps individuais (apenas window_start e window_end)

    Semântica: o consumer deve interpretar o count como "N eventos
    do tipo X ocorreram nesta janela", não como um evento individual.
    """

    event_type: EventType
    count: int
    window_start: datetime
    window_end: datetime
    source: str
    representative_payload: dict[str, Any]
    priority: "Priority"
    aggregate_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __post_init__(self) -> None:
        if self.count < 2:
            raise InvalidEventError("AggregatedEvent.count deve ser >= 2")
        if not isinstance(self.event_type, EventType):
            raise InvalidEventError(
                "AggregatedEvent.event_type precisa ser um EventType"
            )
        if not isinstance(self.representative_payload, dict):
            raise InvalidEventError(
                "AggregatedEvent.representative_payload precisa ser um dict"
            )
        if self.window_start.utcoffset() is None:
            raise InvalidEventError(
                "AggregatedEvent.window_start precisa ser timezone-aware"
            )
        if self.window_end.utcoffset() is None:
            raise InvalidEventError(
                "AggregatedEvent.window_end precisa ser timezone-aware"
            )
        if self.window_end < self.window_start:
            raise InvalidEventError(
                "AggregatedEvent.window_end não pode ser anterior a window_start"
            )

    @property
    def window_duration_seconds(self) -> float:
        """Duração da janela de agregação em segundos."""
        delta = self.window_end - self.window_start
        return delta.total_seconds()
