"""Modelo de prioridade — pertence ao domínio, não à infraestrutura.

O Roblox nunca recalcula prioridade: ela nasce aqui e viaja pronta até
o `Command` final.
"""

from __future__ import annotations

from enum import IntEnum

from src.domain.events import EventType


class Priority(IntEnum):
    """Quanto menor o valor, maior a urgência (P0 é processado primeiro)."""

    P0 = 0  # sistema / controle crítico
    P1 = 1  # presentes e eventos de alto valor
    P2 = 2  # comandos especiais
    P3 = 3  # comentários normais
    P4 = 4  # eventos descartáveis / agregáveis


# baseline inicial — hipótese a validar com dados reais de live (ver
# context/TEST_PLAN.md). não é regra de negócio fechada, é ponto de partida.
DEFAULT_PRIORITY_BY_EVENT_TYPE: dict[EventType, Priority] = {
    EventType.SYSTEM: Priority.P0,
    EventType.GIFT: Priority.P1,
    EventType.MANUAL: Priority.P2,
    EventType.FOLLOW: Priority.P3,
    EventType.COMMENT: Priority.P3,
    EventType.SHARE: Priority.P4,
    EventType.LIKE: Priority.P4,
    EventType.CUSTOM: Priority.P4,
}


def resolve_priority(event_type: EventType) -> Priority:
    """Resolve a prioridade padrão de um tipo de evento.

    Isso é só o baseline — um GIFT específico de alto valor pode
    justificar prioridade diferente da padrão do tipo, mas essa é uma
    regra mais fina (por gift, não por tipo) que ainda não foi
    definida — ver `context/DECISIONS.md`.
    """
    return DEFAULT_PRIORITY_BY_EVENT_TYPE[event_type]
