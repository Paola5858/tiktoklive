"""Agregador de eventos do Event Engine.

Objetivo: reduzir volume de eventos sem destruir significado.

Política de agregação (ver spec):
- Elegível: P4 e floods de comentário (mesmo texto, múltiplos usuários)
- NÃO elegível: P0, P1, P2, gifts (qualquer prioridade), SYSTEM events
- Condição mínima: >= 2 eventos na janela para produzir AggregatedEvent

Algoritmo:
- Janela temporal deslizante por (event_type, source)
- Quando um evento elegível chega, vai para o bucket da janela
- Quando a janela fecha (timeout ou max_bucket_size atingido),
  produz um AggregatedEvent ou libera eventos individuais se count < 2
- Flush periódico via flush_expired() chamado pelo processor

Design de memória:
- _buckets: dict com no máximo N tipos × M fontes distintos
  N tipos = 8 (EventType enum), M fontes = geralmente 1 (tiktok)
  → bounded pelo número de tipos de evento, não pelo número de eventos
- Cada bucket guarda apenas o primeiro evento (representativo) + count
  → sem lista crescente de eventos por bucket
- _bucket_start: quando o bucket foi criado (para TTL da janela)

Decisão de não agregar gifts:
- Gifts têm identidade e valor individual — gift_id é único por transação
- Agregar gifts pode duplicar efeitos no Roblox (ex: spawnar N avatares)
- Exceção: se a spec futura definir explicitamente gifts como agregáveis,
  isso será configurado como política, não hardcoded

Invariante de memória:
- _buckets: O(tipos × fontes) = O(8 × fontes) — praticamente O(1)
- Nenhuma lista de eventos armazenada por bucket
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from src.domain.events import AggregatedEvent, Event, EventType
from src.domain.priorities import Priority

if TYPE_CHECKING:
    from src.engine.config import EngineConfig
    from src.engine.metrics import EngineMetrics

# Tipos que NUNCA devem ser agregados
_NON_AGGREGATABLE_TYPES = frozenset({
    EventType.SYSTEM,
    EventType.MANUAL,
    EventType.GIFT,
})

# Prioridades que NUNCA devem ser agregadas
_NON_AGGREGATABLE_PRIORITIES = frozenset({
    Priority.P0,
    Priority.P1,
    Priority.P2,
})


@dataclass(slots=True)
class _AggregationBucket:
    """Estado de um bucket de agregação para um (event_type, source, priority) específico."""
    representative: Event          # Primeiro evento da janela (payload representativo)
    count: int                     # Total de eventos neste bucket
    window_start_ts: float         # Timestamp monotônico de abertura da janela
    priority: Priority


class EventAggregator:
    """Agrega eventos elegíveis em janelas temporais.

    Usage:
        aggregator = EventAggregator(config, metrics)
        result = aggregator.try_aggregate(event)
        if result is None:
            # evento não é elegível ou janela ainda aberta — processar normalmente
        elif isinstance(result, AggregatedEvent):
            # janela fechou — despachar o aggregate
        else:
            # result é o evento original (bucket criado, aguardando mais eventos)

        # Periodicamente:
        flushed = aggregator.flush_expired()
        for agg in flushed:
            # despachar aggregates expirados
    """

    def __init__(self, config: "EngineConfig", metrics: "EngineMetrics") -> None:
        self._window = config.aggregation_window_seconds
        self._max_bucket = config.aggregation_max_bucket_size
        self._metrics = metrics
        # Chave: (event_type, source, priority_int)
        self._buckets: dict[tuple[str, str, int], _AggregationBucket] = {}

    def is_eligible(self, event: Event, priority: Priority) -> bool:
        """Retorna True se o evento pode ser elegível para agregação.

        Critérios de elegibilidade:
        - Tipo não está na lista non-aggregatable
        - Prioridade não está na lista non-aggregatable
        - Para comentários: elegível mesmo em P3 quando flood configurado
          (a política de flood de comentários é tratada aqui)
        """
        if event.event_type in _NON_AGGREGATABLE_TYPES:
            return False
        if priority in _NON_AGGREGATABLE_PRIORITIES:
            return False
        return True

    def try_aggregate(
        self, event: Event, priority: Priority
    ) -> AggregatedEvent | Event | None:
        """Tenta agregar um evento na janela atual.

        Returns:
            AggregatedEvent: janela fechou por tamanho máximo, retorna o aggregate
            Event: evento não elegível — deve ser processado normalmente
            None: evento foi adicionado ao bucket (aguarda mais eventos ou timeout)

        Nota: retornar None significa que o evento foi "consumido" pelo aggregator.
        O caller NÃO deve enfileirar o evento — será emitido como AggregatedEvent
        via flush_expired() ou quando o bucket atingir max_bucket_size.
        """
        if not self.is_eligible(event, priority):
            return event  # não elegível — retorna para processamento normal

        key = (event.event_type.value, event.source, int(priority))
        now = time.monotonic()

        if key in self._buckets:
            bucket = self._buckets[key]
            window_age = now - bucket.window_start_ts

            if window_age >= self._window:
                # Janela expirou antes de receber este evento
                # Flush o bucket existente e começa um novo com este evento
                agg = self._flush_bucket(key, bucket, now)
                self._buckets[key] = _AggregationBucket(
                    representative=event,
                    count=1,
                    window_start_ts=now,
                    priority=priority,
                )
                return agg  # retorna o aggregate do bucket anterior

            # Janela ainda aberta: adicionar ao bucket
            bucket.count += 1
            if bucket.count >= self._max_bucket:
                # Bucket cheio: flush imediato
                agg = self._flush_bucket(key, bucket, now)
                del self._buckets[key]
                return agg

            return None  # evento absorvido pelo bucket

        else:
            # Primeiro evento para esta chave: abrir bucket
            self._buckets[key] = _AggregationBucket(
                representative=event,
                count=1,
                window_start_ts=now,
                priority=priority,
            )
            return None  # aguardando mais eventos

    def flush_expired(self) -> list[AggregatedEvent | Event]:
        """Fecha buckets com janela expirada e retorna os resultados.

        Deve ser chamado periodicamente pelo engine (ex: a cada 0.5s).
        Buckets com apenas 1 evento retornam o evento original (sem aggregate).
        Buckets com >= 2 eventos retornam um AggregatedEvent.

        Returns:
            Lista de AggregatedEvent (count >= 2) ou Event (count == 1)
            para reprocessamento.
        """
        now = time.monotonic()
        results: list[AggregatedEvent | Event] = []
        expired_keys = [
            k for k, b in self._buckets.items()
            if now - b.window_start_ts >= self._window
        ]

        for key in expired_keys:
            bucket = self._buckets.pop(key)
            result = self._flush_bucket(key, bucket, now)
            if result is not None:
                results.append(result)

        return results

    def pending_buckets(self) -> int:
        """Número de buckets abertos aguardando mais eventos."""
        return len(self._buckets)

    def clear(self) -> None:
        """Abandona todos os buckets pendentes (usado no shutdown)."""
        self._buckets.clear()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _flush_bucket(
        self, key: tuple[str, str, int], bucket: _AggregationBucket, now: float
    ) -> AggregatedEvent | Event | None:
        """Produz o resultado de um bucket sendo fechado.

        - count == 1: retorna o evento representativo (sem agregar um único evento)
        - count >= 2: retorna AggregatedEvent
        """
        if bucket.count == 1:
            # Um único evento não justifica um AggregatedEvent
            return bucket.representative

        event_type_val, source, priority_int = key
        window_start_dt = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - (now - bucket.window_start_ts),
            tz=timezone.utc,
        )
        window_end_dt = datetime.now(timezone.utc)

        self._metrics.record_aggregated(bucket.count)
        self._metrics.record_aggregated_flush()

        return AggregatedEvent(
            event_type=EventType(event_type_val),
            count=bucket.count,
            window_start=window_start_dt,
            window_end=window_end_dt,
            source=source,
            representative_payload=dict(bucket.representative.payload),
            priority=bucket.priority,
        )
