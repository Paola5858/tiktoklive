"""Testes unitários do Dispatcher e consumers.

Cobertos:
- Dispatcher registra e desregistra consumers
- Dispatch chama can_handle antes de handle
- Falha de um consumer não impede outros consumers
- NullConsumer aceita tudo sem efeito
- LoggingConsumer aceita tudo
- Evento sem consumers não lança exception
- Métricas de dispatch são incrementadas corretamente
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from src.domain.events import AggregatedEvent, Event, EventType, EventUser
from src.domain.priorities import Priority
from src.engine.dispatcher import Dispatcher, EventConsumer, LoggingConsumer, NullConsumer
from src.engine.metrics import EngineMetrics


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _user() -> EventUser:
    return EventUser(display_name="tester", external_id="123")


def _event(event_type: EventType = EventType.COMMENT) -> Event:
    return Event(
        event_type=event_type,
        source="tiktok",
        user=_user(),
        payload={"text": "oi"},
        timestamp=datetime.now(timezone.utc),
        received_at=datetime.now(timezone.utc),
    )


def _metrics() -> EngineMetrics:
    return EngineMetrics()


class _RecordingConsumer:
    """Consumer que registra os eventos recebidos para verificação."""

    def __init__(self, name: str = "recording", handle_types: set | None = None):
        self._name = name
        self._handle_types = handle_types
        self.received: list[Event | AggregatedEvent] = []

    @property
    def name(self) -> str:
        return self._name

    def can_handle(self, event: Event | AggregatedEvent) -> bool:
        if self._handle_types is None:
            return True
        event_type = getattr(event, "event_type", None)
        return event_type in self._handle_types

    async def handle(self, event: Event | AggregatedEvent) -> None:
        self.received.append(event)


class _FailingConsumer:
    """Consumer que sempre lança exception."""

    @property
    def name(self) -> str:
        return "failing"

    def can_handle(self, event: Event | AggregatedEvent) -> bool:
        return True

    async def handle(self, event: Event | AggregatedEvent) -> None:
        raise RuntimeError("consumer proposital failure")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_register_consumer():
    d = Dispatcher(_metrics())
    c = _RecordingConsumer()
    d.register(c)
    assert d.consumer_count == 1


def test_register_same_consumer_twice_is_idempotent():
    d = Dispatcher(_metrics())
    c = _RecordingConsumer()
    d.register(c)
    d.register(c)  # Segunda vez não duplica
    assert d.consumer_count == 1


def test_unregister_consumer():
    d = Dispatcher(_metrics())
    c = _RecordingConsumer()
    d.register(c)
    d.unregister(c)
    assert d.consumer_count == 0


def test_unregister_nonexistent_does_not_raise():
    d = Dispatcher(_metrics())
    c = _RecordingConsumer()
    d.unregister(c)  # Não deve lançar


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_calls_handle_on_eligible_consumer():
    d = Dispatcher(_metrics())
    c = _RecordingConsumer()
    d.register(c)

    event = _event()
    await d.dispatch(event)

    assert len(c.received) == 1
    assert c.received[0].event_id == event.event_id


@pytest.mark.asyncio
async def test_dispatch_skips_consumer_that_cannot_handle():
    d = Dispatcher(_metrics())
    c_comment = _RecordingConsumer(handle_types={EventType.COMMENT})
    c_gift = _RecordingConsumer(handle_types={EventType.GIFT})
    d.register(c_comment)
    d.register(c_gift)

    event = _event(EventType.COMMENT)
    await d.dispatch(event)

    assert len(c_comment.received) == 1
    assert len(c_gift.received) == 0  # Não recebe COMMENT


@pytest.mark.asyncio
async def test_dispatch_without_consumers_does_not_raise():
    d = Dispatcher(_metrics())
    event = _event()
    await d.dispatch(event)  # Sem consumers — não deve lançar


@pytest.mark.asyncio
async def test_failing_consumer_does_not_block_other_consumers():
    """Falha de um consumer não impede outros de receber o evento."""
    metrics = _metrics()
    d = Dispatcher(metrics)

    failing = _FailingConsumer()
    recording = _RecordingConsumer(name="second")

    d.register(failing)
    d.register(recording)

    event = _event()
    await d.dispatch(event)

    # O segundo consumer recebeu o evento apesar do primeiro ter falhado
    assert len(recording.received) == 1
    assert metrics.dispatch_failures == 1


@pytest.mark.asyncio
async def test_dispatch_increments_dispatched_metric():
    metrics = _metrics()
    d = Dispatcher(metrics)
    d.register(_RecordingConsumer())

    await d.dispatch(_event())
    assert metrics.events_dispatched == 1


@pytest.mark.asyncio
async def test_dispatch_failure_increments_failure_metric():
    metrics = _metrics()
    d = Dispatcher(metrics)
    d.register(_FailingConsumer())

    await d.dispatch(_event())
    assert metrics.dispatch_failures == 1
    assert metrics.events_dispatched == 0


@pytest.mark.asyncio
async def test_all_consumers_receive_same_event():
    d = Dispatcher(_metrics())
    c1 = _RecordingConsumer(name="c1")
    c2 = _RecordingConsumer(name="c2")
    c3 = _RecordingConsumer(name="c3")
    d.register(c1)
    d.register(c2)
    d.register(c3)

    event = _event()
    await d.dispatch(event)

    for c in [c1, c2, c3]:
        assert len(c.received) == 1
        assert c.received[0].event_id == event.event_id


# ---------------------------------------------------------------------------
# Built-in consumers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_null_consumer_accepts_any_event():
    c = NullConsumer()
    assert c.can_handle(_event()) is True
    await c.handle(_event())  # Não lança


@pytest.mark.asyncio
async def test_logging_consumer_accepts_any_event():
    c = LoggingConsumer()
    assert c.can_handle(_event()) is True
    await c.handle(_event())  # Não lança


@pytest.mark.asyncio
async def test_null_consumer_name():
    assert NullConsumer().name == "null"


@pytest.mark.asyncio
async def test_logging_consumer_name():
    assert LoggingConsumer().name == "logging"


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

def test_snapshot_contains_consumer_names():
    d = Dispatcher(_metrics())
    d.register(_RecordingConsumer(name="alpha"))
    d.register(_RecordingConsumer(name="beta"))

    snap = d.snapshot()
    assert "alpha" in snap["consumers"]
    assert "beta" in snap["consumers"]
    assert snap["consumer_count"] == 2
