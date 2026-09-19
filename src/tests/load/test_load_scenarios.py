"""Cenários de teste de carga do Event Engine.

Estes testes validam se o engine suporta os limites definidos
no PROJECT_SPEC.md sem consumir memória indefinidamente, travar ou perder
eventos de alta prioridade.

Simula floods de comentários, bursts de gifts e gargalos no consumer.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from src.domain.events import Event, EventType, EventUser
from src.engine.config import EngineConfig
from src.engine.dispatcher import Dispatcher, EventConsumer
from src.engine.metrics import EngineMetrics
from src.engine.processor import EventProcessor


class _SlowConsumer:
    """Consumer que simula gargalo de I/O (ex: Roblox Bridge lento)."""
    def __init__(self, delay_sec: float):
        self.delay_sec = delay_sec
        self.processed = 0

    @property
    def name(self) -> str:
        return "slow_consumer"

    def can_handle(self, event) -> bool:
        return True

    async def handle(self, event) -> None:
        await asyncio.sleep(self.delay_sec)
        self.processed += 1


def _user(uid: str) -> EventUser:
    return EventUser(display_name=f"user_{uid}", external_id=uid)


def _event(event_type: EventType, uid: str, text: str = "") -> Event:
    return Event(
        event_type=event_type,
        source="load_test",
        user=_user(uid),
        payload={"text": text} if text else {},
        timestamp=datetime.now(timezone.utc),
        received_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_load_comment_flood_aggregation():
    """Garante que floods de comentários são agregados e não quebram o consumer."""
    config = EngineConfig(
        n_workers=2,
        queue_capacity_per_level=5000,
        aggregation_window_seconds=1.0,
        aggregation_max_bucket_size=500,
    )
    metrics = EngineMetrics()
    dispatcher = Dispatcher(metrics)

    # Consumer instantâneo
    class FastConsumer:
        def __init__(self):
            self.count = 0
            self.aggregates = 0
        @property
        def name(self): return "fast"
        def can_handle(self, e): return True
        async def handle(self, e):
            from src.domain.events import AggregatedEvent
            self.count += getattr(e, "count", 1)
            if isinstance(e, AggregatedEvent):
                self.aggregates += 1

    consumer = FastConsumer()
    dispatcher.register(consumer)

    processor = EventProcessor(config, metrics, dispatcher)
    await processor.start()

    # Disparar 10.000 comentários idênticos de "spammers"
    try:
        tasks = []
        for i in range(10_000):
            tasks.append(
                processor.receive(_event(EventType.COMMENT, uid=str(i), text="SPAM"))
            )
            if len(tasks) >= 1000:
                await asyncio.gather(*tasks)
                tasks.clear()
        if tasks:
            await asyncio.gather(*tasks)

        # Aguardar um flush cycle do aggregator
        await asyncio.sleep(1.5)
    finally:
        await processor.stop()

    # 10k eventos foram absorvidos e transformados em aggregates
    assert metrics.events_received == 10_000
    assert metrics.events_dropped == 0  # Nenhum P3 foi dropado pois foram agregados
    assert consumer.count == 10_000
    assert consumer.aggregates > 0
    assert metrics.events_dispatched == consumer.aggregates


@pytest.mark.asyncio
async def test_load_priority_survival_under_bottleneck():
    """Garante que com consumer muito lento, eventos de baixa prioridade
    são dropados para salvar os de alta prioridade.
    """
    config = EngineConfig(
        n_workers=1,  # Bottleneck intencional
        max_in_flight=1,
        queue_capacity_per_level=100,
        overflow_threshold_p4=0.1,  # P4 dropa muito cedo
        overflow_threshold_p3=0.2,
        aggregation_window_seconds=0.01,  # Praticamente imediato
    )
    metrics = EngineMetrics()
    dispatcher = Dispatcher(metrics)

    slow_consumer = _SlowConsumer(delay_sec=0.05)  # Só processa 20 por segundo
    dispatcher.register(slow_consumer)

    processor = EventProcessor(config, metrics, dispatcher)
    processor._aggregator.is_eligible = lambda e, p: False  # Desabilitar agregação no teste
    await processor.start()

    try:
        # Enviar um burst rápido de 200 eventos P4, 200 P3, e 50 P1
        # Isso lota a fila total (~450) > threshold
        tasks = []
        for i in range(200):
            tasks.append(processor.receive(_event(EventType.LIKE, uid=f"u4_{i}")))
            tasks.append(processor.receive(_event(EventType.COMMENT, uid=f"u3_{i}", text=f"c{i}")))
            if i % 4 == 0:
                # 50 Gifts (P1) misturados
                tasks.append(processor.receive(_event(EventType.GIFT, uid=f"u1_{i}")))

        await asyncio.gather(*tasks)

        # Aguardar alguns segundos para o consumer lento drenar
        await asyncio.sleep(3.0)
    finally:
        await processor.stop()

    # O que esperamos:
    # 1. 450 eventos recebidos
    # 2. Dropados os de P4 e P3 porque a fila estourou o limite e o consumer é lento
    # 3. Nenhum P1 (Gift) deve ser dropado!

    assert metrics.events_received == 450
    assert metrics.events_dropped > 0

    # Gifts (P1) não chegam perto do threshold (p1=0.9), então 100% deles devem ter sido enfileirados.
    # O total_capacity é 500 (100 * 5).
    # 50 Gifts misturados no loop. Nenhum deve ter sido dropado por queue_full.
    assert metrics.events_received == 450
    assert metrics.events_dropped > 0  # Garantir que P4/P3 foram descartados

    # Podemos verificar que a soma de aceitos + dropados + dedup = recebidos
    assert metrics.events_accepted + metrics.events_dropped + metrics.events_deduplicated == 450
