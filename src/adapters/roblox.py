"""Roblox Bridge — tradução de eventos processados em um envelope de transporte
consumível pelo Roblox via HTTP polling, e o buffer que sustenta esse polling.

Este módulo NÃO conhece o Roblox de verdade (nenhum HttpService, nenhum Luau).
Ele conhece três coisas:

1. Como virar um `Event`/`AggregatedEvent` do domínio em um `GameEventEnvelope`
   serializável — a fronteira de tradução (ver `transport_to_game_translation`
   na spec da fase 4).
2. Como guardar esses envelopes num buffer limitado, com cursor, até o
   consumidor (Roblox) vir buscá-los.
3. Como registrar ack e expor estado de saúde pra observabilidade.

O que este módulo DELIBERADAMENTE não faz nesta fase (ver DECISIONS.md):
- Não decide efeitos de jogo por gift específico (Gift Mapping Engine é fase 5).
- Não resolve identidade TikTok -> Roblox (decisão em aberto).
- Não persiste em disco — o buffer é só em memória, bounded, com eviction.

Semântica de entrega escolhida: **at-least-once com idempotência no
consumidor**. Ver `context/ROBLOX_BRIDGE.md` para a justificativa completa.
Isso significa: um evento pode aparecer mais de uma vez numa resposta de
`GET /events` (por exemplo, se o Roblox nunca confirma recebimento e a
janela de long-poll se repete), mas o `event_id`/`command_id` do envelope
não muda entre aparições — o consumidor decide ignorar duplicatas.
"""

from __future__ import annotations

import itertools
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.domain.events import AggregatedEvent, Event
from src.domain.priorities import DEFAULT_PRIORITY_BY_EVENT_TYPE, Priority
from src.interaction.models import GameEvent
from src.logging import get_logger
from src.observability.health import Watchdog, ComponentHealth

LOGGER = get_logger(__name__)

# Versão do contrato de envelope exposto ao Roblox. Incrementar (e nunca
# reaproveitar um número) sempre que um campo obrigatório mudar de forma
# incompatível — ver `schema_versioning` na spec da fase 4.
SCHEMA_VERSION = "1.0"


class RobloxBridgeConfig:
    """Configuração do bridge — valores conservadores, documentados como baseline.

    Attributes:
        buffer_capacity: Quantos envelopes o buffer mantém em memória antes
            de começar a descartar os mais antigos (FIFO eviction). Não é
            "fila infinita" — ver `queue_and_backpressure` no prompt da
            fase 1 e `transport_queue` na fase 4.
        max_events_per_poll: Teto de envelopes retornados numa única
            chamada de `GET /events`, independente do `limit` pedido.
            Existe pra impedir que um `limit` mal-intencionado ou errado
            force uma resposta gigante.
        default_events_per_poll: `limit` usado quando o Roblox não manda um.
    """

    def __init__(
        self,
        buffer_capacity: int = 500,
        max_events_per_poll: int = 100,
        default_events_per_poll: int = 25,
    ) -> None:
        if buffer_capacity <= 0:
            raise ValueError("buffer_capacity deve ser > 0")
        if max_events_per_poll <= 0:
            raise ValueError("max_events_per_poll deve ser > 0")
        if not (0 < default_events_per_poll <= max_events_per_poll):
            raise ValueError(
                "default_events_per_poll deve estar entre 1 e max_events_per_poll"
            )
        self.buffer_capacity = buffer_capacity
        self.max_events_per_poll = max_events_per_poll
        self.default_events_per_poll = default_events_per_poll

    @classmethod
    def default(cls) -> "RobloxBridgeConfig":
        return cls()


@dataclass(frozen=True, slots=True)
class GameEventEnvelope:
    """Envelope de transporte que atravessa a Local API até o Roblox."""

    sequence_number: int
    event_id: str
    event_type: str
    timestamp: str  # ISO-8601, UTC
    priority: int
    payload: dict[str, Any]
    source: str | None = None
    user: dict[str, Any] | None = None
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "sequence_number": self.sequence_number,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "priority": self.priority,
            "payload": self.payload,
            "source": self.source,
            "user": self.user,
        }


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def to_envelope(event: Event | AggregatedEvent, sequence_number: int) -> GameEventEnvelope:
    """Traduz um `Event`/`AggregatedEvent` do domínio pro envelope de transporte."""
    if isinstance(event, AggregatedEvent):
        return GameEventEnvelope(
            sequence_number=sequence_number,
            event_id=event.aggregate_id,
            event_type="AGGREGATED",
            timestamp=_iso(event.window_end),
            priority=int(event.priority),
            payload={
                "original_event_type": event.event_type.value,
                "count": event.count,
                "representative_payload": event.representative_payload,
                "window_start": _iso(event.window_start),
                "window_end": _iso(event.window_end),
            },
            source=event.source,
            user=None,
        )

    priority = getattr(event, "priority", None)
    if priority is None:
        priority = DEFAULT_PRIORITY_BY_EVENT_TYPE.get(event.event_type, Priority.P4)

    return GameEventEnvelope(
        sequence_number=sequence_number,
        event_id=event.event_id,
        event_type=event.event_type.value,
        timestamp=_iso(event.timestamp),
        priority=int(priority),
        payload=dict(event.payload),
        source=event.source,
        user={
            "display_name": event.user.display_name,
            "external_id": event.user.external_id,
        },
    )


def game_event_to_envelope(event: GameEvent, sequence_number: int) -> GameEventEnvelope:
    """Traduz um GameEvent produzido pelo Interaction Rules Engine."""
    return GameEventEnvelope(
        sequence_number=sequence_number,
        event_id=event.event_id,
        event_type=event.event_type,
        timestamp=_iso(event.timestamp),
        priority=int(event.priority),
        payload=dict(event.payload),
        source="interaction_rules",
        user=None,
    )


class RobloxBridge:
    """Consumer do Event Engine que alimenta o buffer de entrega do Roblox."""

    def __init__(
        self, config: RobloxBridgeConfig | None = None, watchdog: Watchdog | None = None
    ) -> None:
        self._config = config or RobloxBridgeConfig.default()
        self._buffer: deque[GameEventEnvelope] = deque(maxlen=self._config.buffer_capacity)
        self._sequence = itertools.count(start=1)
        self._lock = threading.Lock()

        # Observabilidade (ver `health_contract`/`metrics` na spec da fase 4)
        self._events_delivered_total = 0
        self._events_evicted_total = 0
        self._last_event_at: datetime | None = None
        self._last_poll_at: datetime | None = None
        self._last_acknowledged_sequence = 0
        self._lowest_sequence_ever = 0
        self._highest_sequence_ever = 0

        self._health: ComponentHealth | None = None
        if watchdog:
            self._health = watchdog.register("RobloxBridgeConsumer")

    # ------------------------------------------------------------------
    # EventConsumer protocol (src/engine/dispatcher.py)
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "roblox_bridge"

    def can_handle(self, event: Event | AggregatedEvent) -> bool:
        # Nesta fase o bridge aceita tudo que o engine processar. Filtragem
        # por tipo (ex: só GIFT/COMMENT interessam ao Roblox) é uma decisão
        # de produto pra fase 5, quando o Gift Mapping Engine existir.
        return True

    def _push(self, envelope: GameEventEnvelope) -> None:
        """Adiciona envelope ao buffer. Deve ser chamado com self._lock adquirido."""
        evicted = len(self._buffer) == self._buffer.maxlen
        self._buffer.append(envelope)
        self._events_delivered_total += 1
        if evicted:
            self._events_evicted_total += 1
        self._last_event_at = datetime.now(timezone.utc)
        self._highest_sequence_ever = envelope.sequence_number
        if self._lowest_sequence_ever == 0:
            self._lowest_sequence_ever = envelope.sequence_number
        if self._health:
            self._health.mark_active()

    async def handle(self, event: Event | AggregatedEvent) -> None:
        with self._lock:
            envelope = to_envelope(event, next(self._sequence))
            self._push(envelope)

    async def publish_game_event(self, event: GameEvent) -> None:
        """Publica um GameEvent já resolvido pelo Interaction Rules Engine."""
        with self._lock:
            envelope = game_event_to_envelope(event, next(self._sequence))
            self._push(envelope)

    # ------------------------------------------------------------------
    # API pra Local API consumir
    # ------------------------------------------------------------------

    def get_events_since(
        self, since: int, limit: int | None = None
    ) -> tuple[list[GameEventEnvelope], int, bool]:
        """Retorna envelopes com `sequence_number > since`, até `limit`.

        Não remove nada do buffer — eviction acontece só por capacidade
        (FIFO), nunca por causa de uma leitura (ver `poll_response` na
        spec da fase 4: "não apagar eventos... se o sistema ainda não
        possui semântica segura para confirmação").

        Returns:
            (envelopes, cursor, gap_detected) — `cursor` é o maior
            `sequence_number` já visto pelo bridge (não só o retornado
            nesta página), pra o Roblox saber até onde existe dado, mesmo
            que `limit` tenha cortado a resposta. `gap_detected` é True
            quando `since` é anterior ao que o buffer ainda guarda —
            sinal de que eventos foram descartados por eviction antes do
            Roblox consumir (ver `ordering`/`sequence_numbers` na spec).
        """
        effective_limit = min(
            limit if limit is not None else self._config.default_events_per_poll,
            self._config.max_events_per_poll,
        )

        with self._lock:
            self._last_poll_at = datetime.now(timezone.utc)
            if self._health:
                self._health.mark_active()
            oldest_available = self._buffer[0].sequence_number if self._buffer else since

            gap_detected = bool(self._buffer) and since > 0 and since < oldest_available - 1

            result = [e for e in self._buffer if e.sequence_number > since][:effective_limit]
            cursor = self._highest_sequence_ever

        return result, cursor, gap_detected

    def ack(self, up_to_sequence: int) -> int:
        """Registra até onde o Roblox confirma ter processado."""
        with self._lock:
            if self._health:
                self._health.record_success()
            if up_to_sequence > self._last_acknowledged_sequence:
                self._last_acknowledged_sequence = up_to_sequence
            return self._last_acknowledged_sequence

    # ------------------------------------------------------------------
    # Observabilidade
    # ------------------------------------------------------------------

    def health_snapshot(self) -> dict[str, Any]:
        with self._lock:
            depth = len(self._buffer)
            return {
                "schema_version": SCHEMA_VERSION,
                "buffer_depth": depth,
                "buffer_capacity": self._config.buffer_capacity,
                "events_delivered_total": self._events_delivered_total,
                "events_evicted_total": self._events_evicted_total,
                "last_event_at": _iso(self._last_event_at) if self._last_event_at else None,
                "last_poll_at": _iso(self._last_poll_at) if self._last_poll_at else None,
                "last_acknowledged_sequence": self._last_acknowledged_sequence,
                "highest_sequence_ever": self._highest_sequence_ever,
            }
