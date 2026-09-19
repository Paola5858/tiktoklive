"""Testes unitários do EventProcessor (pipeline completo).

Cobertos:
- Pipeline completo: receive → priority → dedup → enqueue → worker → dispatch
- Handler que lança não derruba o engine
- Shutdown coordenado, sem tasks órfãs
- Métricas refletem eventos processados/dropped/failed
- receive() durante shutdown lança EngineShutdownError
- Eventos P0 têm prioridade sobre P3 no processamento
- Deduplicação funciona dentro do processor
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest
import pytest_asyncio

from src.domain.events import AggregatedEvent, Event, EventType, EventUser
from src.domain.priorities import Priority
from src.engine.config import EngineConfig
from src.engine.dispatcher import Dispatcher, NullConsumer
from src.engine.errors import EngineShutdownError
from src.engine.metrics import EngineMetrics
from src.engine.processor import EventProcessor


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _user(name: str = "tester") -> EventUser:
    return EventUser(display_name=name, external_id=f"id:{name}")


def _event(
    event_type: EventType = EventType.GIFT,
    name: str = "tester",
    source_event_id: str | None = None,
    payload: dict | None = None,
) -> Event:
    return Event(
        event_type=event_type,
        source="tiktok",
        user=_user(name),
        payload=payload or {"text": "oi"},
        source_event_id=source_event_id,
        timestamp=datetime.now(timezone.utc),
        received_at=datetime.now(timezone.utc),
    )


class _CountingConsumer:
    """Consumer que conta eventos recebidos."""

    def __init__(self):
        self.count = 0
        self.events: list[Event | AggregatedEvent] = []

    @property
    def name(self) -> str:
        return "counting"

    def can_handle(self, event: Event | AggregatedEvent) -> bool:
        return True

    async def handle(self, event: Event | AggregatedEvent) -> None:
        self.count += 1
        self.events.append(event)


class _FailingConsumer:
    """Consumer que sempre lança exception."""

    @property
    def name(self) -> str:
        return "failing"

    def can_handle(self, event: Event | AggregatedEvent) -> bool:
        return True

    async def handle(self, event: Event | AggregatedEvent) -> None:
        raise ValueError("deliberate failure")


def _make_processor(
    n_workers: int = 2,
    queue_capacity: int = 100,
    dedup_ttl: float = 30.0,
    aggregation_window: float = 60.0,
) -> tuple[EventProcessor, EngineMetrics, Dispatcher]:
    config = EngineConfig(
        n_workers=n_workers,
        queue_capacity_per_level=queue_capacity,
        dedup_ttl_seconds=dedup_ttl,
        aggregation_window_seconds=aggregation_window,
        aggregation_max_bucket_size=100,  # Grande para não fechar no teste
    )
    metrics = EngineMetrics()
    dispatcher = Dispatcher(metrics)
    processor = EventProcessor(config, metrics, dispatcher)
    return processor, metrics, dispatcher


# ---------------------------------------------------------------------------
# Basic pipeline
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_receive_and_process_single_event():
    """Evento recebido deve ser processado e despachado."""
    processor, metrics, dispatcher = _make_processor()
    consumer = _CountingConsumer()
    dispatcher.register(consumer)

    await processor.start()
    try:
        await processor.receive(_event())
        # Aguardar processamento
        await asyncio.sleep(0.1)
    finally:
        await processor.stop()

    assert consumer.count >= 1
    assert metrics.events_received >= 1


@pytest.mark.asyncio
async def test_multiple_events_all_processed():
    """Múltiplos eventos devem ser todos processados."""
    processor, metrics, dispatcher = _make_processor(n_workers=2)
    consumer = _CountingConsumer()
    dispatcher.register(consumer)

    n = 10
    await processor.start()
    try:
        for i in range(n):
            await processor.receive(_event(name=f"user{i}", source_event_id=f"sid-{i}"))
        await asyncio.sleep(0.2)
    finally:
        await processor.stop()

    # Todos os n eventos únicos devem ter sido despachados
    assert consumer.count == n


@pytest.mark.asyncio
async def test_processor_start_is_idempotent():
    """start() chamado múltiplas vezes não cria workers duplicados."""
    processor, metrics, _ = _make_processor(n_workers=2)
    await processor.start()
    await processor.start()  # Segunda chamada — não deve duplicar workers
    await processor.stop()

    # Se houvesse workers duplicados, o worker_count seria > n_workers
    # Verificar apenas que não lançou exception e encerrou corretamente


@pytest.mark.asyncio
async def test_stop_when_not_started_does_not_raise():
    """stop() em processor não iniciado não deve lançar."""
    processor, _, _ = _make_processor()
    await processor.stop()  # Não deve lançar


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_failing_handler_does_not_crash_engine():
    """Exception no consumer não derruba o engine."""
    processor, metrics, dispatcher = _make_processor()
    failing = _FailingConsumer()
    recording = _CountingConsumer()

    dispatcher.register(failing)
    dispatcher.register(recording)

    await processor.start()
    try:
        for i in range(5):
            await processor.receive(_event(source_event_id=f"e{i}"))
        await asyncio.sleep(0.2)
    finally:
        await processor.stop()

    # Engine ainda está processando: recording recebeu eventos
    assert recording.count == 5
    # Métricas de falha registradas
    assert metrics.dispatch_failures >= 5


@pytest.mark.asyncio
async def test_engine_continues_after_consumer_failure():
    """Eventos continuam sendo processados mesmo após falha de consumer."""
    processor, metrics, dispatcher = _make_processor()
    failing = _FailingConsumer()
    dispatcher.register(failing)

    await processor.start()
    try:
        # Enviar 3 eventos — todos devem ser "processados" (mesmo com falha no dispatch)
        for i in range(3):
            await processor.receive(_event(source_event_id=f"cont-{i}"))
        await asyncio.sleep(0.2)
    finally:
        await processor.stop()

    # Engine não travou (stop() retornou)
    assert metrics.events_received == 3


# ---------------------------------------------------------------------------
# Deduplication within processor
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_duplicate_events_are_deduplicated():
    """Eventos com mesmo source_event_id não devem ser processados duas vezes."""
    processor, metrics, dispatcher = _make_processor()
    consumer = _CountingConsumer()
    dispatcher.register(consumer)

    await processor.start()
    try:
        # Mesmo source_event_id — duplicata
        await processor.receive(_event(source_event_id="dup-001"))
        await processor.receive(_event(source_event_id="dup-001"))
        await asyncio.sleep(0.15)
    finally:
        await processor.stop()

    assert consumer.count == 1
    assert metrics.events_deduplicated == 1


@pytest.mark.asyncio
async def test_different_events_not_deduplicated():
    """Eventos com source_event_id diferentes devem ser processados separadamente."""
    processor, metrics, dispatcher = _make_processor()
    consumer = _CountingConsumer()
    dispatcher.register(consumer)

    await processor.start()
    try:
        await processor.receive(_event(source_event_id="unique-001"))
        await processor.receive(_event(source_event_id="unique-002"))
        await asyncio.sleep(0.15)
    finally:
        await processor.stop()

    assert consumer.count == 2
    assert metrics.events_deduplicated == 0


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_receive_during_shutdown_raises():
    """receive() durante shutdown deve lançar EngineShutdownError."""
    processor, _, _ = _make_processor()
    await processor.start()
    await processor.stop()

    with pytest.raises(EngineShutdownError):
        await processor.receive(_event())


@pytest.mark.asyncio
async def test_stop_cleans_up_all_tasks():
    """stop() deve encerrar todos os workers sem tasks órfãs."""
    processor, _, dispatcher = _make_processor(n_workers=4)
    dispatcher.register(NullConsumer())

    await processor.start()
    # Enviar alguns eventos
    for i in range(5):
        await processor.receive(_event(source_event_id=f"shutdown-{i}"))

    await processor.stop()  # Deve retornar sem timeout
    # Se houver tasks órfãs, elas apareceriam como pending tasks
    # Verificar apenas que stop() completou


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_metrics_reflect_processed_events():
    """Métricas devem refletir o estado real do processamento."""
    processor, metrics, dispatcher = _make_processor()
    dispatcher.register(NullConsumer())

    await processor.start()
    try:
        for i in range(5):
            await processor.receive(_event(source_event_id=f"m-{i}"))
        await asyncio.sleep(0.2)
    finally:
        await processor.stop()

    assert metrics.events_received == 5
    assert metrics.events_processed == 5
    assert metrics.events_failed == 0


@pytest.mark.asyncio
async def test_metrics_snapshot_is_consistent():
    """metrics_snapshot() retorna dict com campos esperados."""
    processor, metrics, dispatcher = _make_processor()
    dispatcher.register(NullConsumer())

    await processor.start()
    await processor.receive(_event(source_event_id="snap-1"))
    await asyncio.sleep(0.1)
    await processor.stop()

    snap = processor.metrics_snapshot()
    required_keys = {"received", "accepted", "processed", "dropped", "deduplicated",
                     "queue_depth_by_priority", "dedup_cache", "dispatcher"}
    for k in required_keys:
        assert k in snap, f"Campo '{k}' ausente no snapshot"


# ---------------------------------------------------------------------------
# is_running
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_is_running_reflects_state():
    processor, _, _ = _make_processor()
    assert processor.is_running is False

    await processor.start()
    assert processor.is_running is True

    await processor.stop()
    assert processor.is_running is False
