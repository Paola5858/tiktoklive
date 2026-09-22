"""Cenários de chaos e testes de saturação (Fase 10)."""

import asyncio
import pytest
from datetime import datetime, timezone

from src.engine.config import EngineConfig
from src.engine.metrics import EngineMetrics
from src.engine.dispatcher import Dispatcher, NullConsumer, EventConsumer
from src.engine.processor import EventProcessor
from src.observability.health import Watchdog
from src.domain.events import Event, EventType, EventUser


class SlowConsumer(EventConsumer):
    def __init__(self, delay: float):
        self._delay = delay
        self.processed = 0

    @property
    def name(self) -> str:
        return "slow_consumer"

    def can_handle(self, event: Event) -> bool:
        return True

    async def handle(self, event: Event) -> None:
        await asyncio.sleep(self._delay)
        self.processed += 1


class FailingConsumer(EventConsumer):
    def __init__(self, fail_rate: float):
        self._fail_rate = fail_rate
        self.calls = 0

    @property
    def name(self) -> str:
        return "failing_consumer"

    def can_handle(self, event: Event) -> bool:
        return True

    async def handle(self, event: Event) -> None:
        self.calls += 1
        # Simplistic failing for the chaos test (fails every alternative or so)
        if self.calls % 2 == 0:
            raise RuntimeError("Injected consumer failure")


@pytest.mark.asyncio
async def test_chaos_slow_consumer_queue_overflow():
    """Valida overflow handling com consumer extremamente lento (saturação)."""
    # 5 eventos por queue, 1 worker, consumer demora 0.05s
    # Usa GIFT (P1, não agregável) para testar overflow de fila pura
    config = EngineConfig(
        queue_capacity_per_level=5,
        n_workers=1,
        max_in_flight=16,
    )
    metrics = EngineMetrics()
    dispatcher = Dispatcher(metrics)

    # Adiciona slow consumer
    slow = SlowConsumer(delay=0.05)
    dispatcher.register(slow)

    processor = EventProcessor(config, metrics, dispatcher)
    await processor.start()

    # Injeta um flood de 30 eventos P1 (GIFT) rapidamente
    # A fila suporta 5 * 5 = 25 eventos total, threshold pra drop P1 é 90% (22 eventos)
    # P1 não é agregável, vai direto pra fila. Deve aceitar até ~22 e rejeitar o resto.

    user = EventUser(external_id="u", display_name="u")
    accepted = 0

    for i in range(30):
        ev = Event(
            event_id=f"p1_{i}",
            source_event_id=f"gift_{i}",
            event_type=EventType.GIFT,
            source="test",
            timestamp=datetime.now(timezone.utc),
            user=user,
            payload={"gift_id": i, "gift_value": 100, "repeat_count": 1}
        )
        # GIFT → P1, não agregável
        try:
            await processor.receive(ev)
            accepted += 1
        except Exception as e:
            if "QueueFullError" in str(type(e)):
                pass

    # Espera alguns processarem
    await asyncio.sleep(0.5)
    await processor.stop()

    snap = metrics.snapshot()
    # Verifica se drops ocorreram devido à saturação
    assert snap["dropped"] > 0
    assert snap["overflow"] > 0
    assert slow.processed > 0


@pytest.mark.asyncio
async def test_chaos_failing_consumer_isolation():
    """Valida que falhas frequentes em um consumer não afetam o processamento de outros."""
    config = EngineConfig(n_workers=2)
    metrics = EngineMetrics()
    dispatcher = Dispatcher(metrics)

    failer = FailingConsumer(fail_rate=0.5)
    slow = SlowConsumer(delay=0.01)

    dispatcher.register(failer)
    dispatcher.register(slow)

    processor = EventProcessor(config, metrics, dispatcher)
    await processor.start()

    user = EventUser(external_id="u", display_name="u")

    for i in range(10):
        ev = Event(
            event_id=f"e_{i}",
            source_event_id=f"gift_{i}",
            event_type=EventType.GIFT,
            source="test",
            timestamp=datetime.now(timezone.utc),
            user=user,
            payload={"gift_id": i}
        )
        await processor.receive(ev)

    await asyncio.sleep(0.3)
    await processor.stop()

    snap = metrics.snapshot()
    assert failer.calls == 10
    assert snap["dispatch_failures"] == 5  # Metade falhou no dispatcher due to FailingConsumer
    assert slow.processed == 10  # Mas todos foram entregues para o slow consumer
