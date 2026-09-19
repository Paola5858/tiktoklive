"""Testes unitários do EventAggregator.

Cobertos:
- Eventos P4 são agregados na janela
- Contagem é preservada corretamente
- Bucket flush quando max_bucket_size atingido
- Eventos P0/P1/P2 não são agregados
- Gifts (EventType.GIFT) nunca são agregados
- Janela expirada produz flush
- Evento único (count=1) retorna o evento original (não AggregatedEvent)
- Eventos não elegíveis retornam imediatamente
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from src.domain.events import AggregatedEvent, Event, EventType, EventUser
from src.domain.priorities import Priority
from src.engine.aggregator import EventAggregator
from src.engine.config import EngineConfig
from src.engine.metrics import EngineMetrics


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _user(name: str = "tester") -> EventUser:
    return EventUser(display_name=name, external_id=f"id:{name}")


def _event(
    event_type: EventType = EventType.LIKE,
    name: str = "tester",
    payload: dict | None = None,
) -> Event:
    return Event(
        event_type=event_type,
        source="tiktok",
        user=_user(name),
        payload=payload or {},
        timestamp=datetime.now(timezone.utc),
        received_at=datetime.now(timezone.utc),
    )


def _aggregator(
    window_seconds: float = 60.0,
    max_bucket_size: int = 10,
) -> EventAggregator:
    config = EngineConfig(
        aggregation_window_seconds=window_seconds,
        aggregation_max_bucket_size=max_bucket_size,
    )
    metrics = EngineMetrics()
    return EventAggregator(config, metrics)


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------

def test_p4_event_is_eligible():
    agg = _aggregator()
    e = _event(EventType.LIKE)
    assert agg.is_eligible(e, Priority.P4) is True


def test_p0_event_is_not_eligible():
    agg = _aggregator()
    e = _event(EventType.MANUAL)
    assert agg.is_eligible(e, Priority.P0) is False


def test_p1_event_is_not_eligible():
    agg = _aggregator()
    e = _event(EventType.GIFT)
    assert agg.is_eligible(e, Priority.P1) is False


def test_gift_event_is_never_eligible():
    """GIFT é non-aggregatable por tipo, independente da prioridade."""
    agg = _aggregator()
    e = _event(EventType.GIFT)
    # Mesmo se fosse P4 (hipoteticamente), GIFT não é elegível
    assert agg.is_eligible(e, Priority.P4) is False


def test_system_event_is_never_eligible():
    """SYSTEM events nunca devem ser agregados."""
    agg = _aggregator()
    e = _event(EventType.SYSTEM)
    assert agg.is_eligible(e, Priority.P0) is False


def test_manual_event_is_not_eligible_at_p2():
    agg = _aggregator()
    e = _event(EventType.MANUAL)
    assert agg.is_eligible(e, Priority.P2) is False


# ---------------------------------------------------------------------------
# Aggregation behavior
# ---------------------------------------------------------------------------

def test_non_eligible_event_returned_immediately():
    """Evento não elegível é retornado imediatamente como Event."""
    agg = _aggregator()
    e = _event(EventType.GIFT)
    result = agg.try_aggregate(e, Priority.P1)
    assert result is e  # Retorno do evento original


def test_first_eligible_event_opens_bucket_returns_none():
    """Primeiro evento elegível abre o bucket e retorna None."""
    agg = _aggregator(window_seconds=60.0)
    e = _event(EventType.LIKE)
    result = agg.try_aggregate(e, Priority.P4)
    assert result is None  # Absorvido pelo bucket
    assert agg.pending_buckets() == 1


def test_second_eligible_event_same_type_absorbed():
    """Segundo evento do mesmo tipo vai para o mesmo bucket."""
    agg = _aggregator(window_seconds=60.0, max_bucket_size=10)
    e1 = _event(EventType.LIKE, name="user1")
    e2 = _event(EventType.LIKE, name="user2")

    r1 = agg.try_aggregate(e1, Priority.P4)
    r2 = agg.try_aggregate(e2, Priority.P4)

    assert r1 is None
    assert r2 is None
    assert agg.pending_buckets() == 1  # Apenas 1 bucket


def test_max_bucket_size_triggers_flush():
    """Bucket com max_bucket_size eventos faz flush e retorna AggregatedEvent."""
    agg = _aggregator(window_seconds=60.0, max_bucket_size=3)

    events = [_event(EventType.LIKE, name=f"u{i}") for i in range(3)]
    results = []
    for e in events:
        r = agg.try_aggregate(e, Priority.P4)
        if r is not None:
            results.append(r)

    assert len(results) == 1
    agg_event = results[0]
    assert isinstance(agg_event, AggregatedEvent)
    assert agg_event.count == 3
    assert agg_event.event_type == EventType.LIKE


def test_aggregated_event_preserves_count():
    """AggregatedEvent preserva a contagem correta de eventos."""
    agg = _aggregator(window_seconds=60.0, max_bucket_size=5)

    for i in range(5):
        result = agg.try_aggregate(_event(EventType.LIKE, name=f"u{i}"), Priority.P4)

    # O flush do último evento retorna o aggregate
    assert isinstance(result, AggregatedEvent)
    assert result.count == 5


def test_different_event_types_have_separate_buckets():
    """Eventos de tipos diferentes têm buckets separados."""
    agg = _aggregator(window_seconds=60.0, max_bucket_size=10)

    e_like = _event(EventType.LIKE)
    e_share = _event(EventType.SHARE)

    agg.try_aggregate(e_like, Priority.P4)
    agg.try_aggregate(e_share, Priority.P4)

    assert agg.pending_buckets() == 2


# ---------------------------------------------------------------------------
# flush_expired
# ---------------------------------------------------------------------------

def test_flush_expired_returns_aggregated_for_multiple_events(monkeypatch):
    """flush_expired() retorna AggregatedEvent quando count >= 2."""
    agg = _aggregator(window_seconds=0.01)  # Janela muito pequena

    e1 = _event(EventType.LIKE, name="u1")
    e2 = _event(EventType.LIKE, name="u2")
    agg.try_aggregate(e1, Priority.P4)
    agg.try_aggregate(e2, Priority.P4)

    # Simular passagem do tempo além da janela
    original_time = time.monotonic

    def fake_time():
        return original_time() + 1.0  # 1 segundo à frente

    monkeypatch.setattr("src.engine.aggregator.time.monotonic", fake_time)

    results = agg.flush_expired()
    assert len(results) == 1
    assert isinstance(results[0], AggregatedEvent)
    assert results[0].count == 2


def test_flush_expired_returns_event_for_single_event(monkeypatch):
    """flush_expired() retorna o Event original quando count == 1."""
    agg = _aggregator(window_seconds=0.01)

    e = _event(EventType.LIKE, name="lonely")
    agg.try_aggregate(e, Priority.P4)

    original_time = time.monotonic

    def fake_time():
        return original_time() + 1.0

    monkeypatch.setattr("src.engine.aggregator.time.monotonic", fake_time)

    results = agg.flush_expired()
    assert len(results) == 1
    # Evento único retorna como Event original, não AggregatedEvent
    assert isinstance(results[0], Event)
    assert results[0].event_id == e.event_id


def test_flush_expired_does_not_flush_open_windows():
    """flush_expired() não fecha buckets ainda dentro da janela."""
    agg = _aggregator(window_seconds=60.0)  # Janela de 60s

    e = _event(EventType.LIKE)
    agg.try_aggregate(e, Priority.P4)

    results = agg.flush_expired()
    assert len(results) == 0
    assert agg.pending_buckets() == 1


def test_flush_expired_removes_closed_buckets(monkeypatch):
    """Buckets fechados por flush_expired() são removidos do estado interno."""
    agg = _aggregator(window_seconds=0.01)

    agg.try_aggregate(_event(EventType.LIKE), Priority.P4)
    agg.try_aggregate(_event(EventType.LIKE), Priority.P4)

    original_time = time.monotonic

    def fake_time():
        return original_time() + 1.0

    monkeypatch.setattr("src.engine.aggregator.time.monotonic", fake_time)

    agg.flush_expired()
    assert agg.pending_buckets() == 0


# ---------------------------------------------------------------------------
# Clear
# ---------------------------------------------------------------------------

def test_clear_removes_all_pending_buckets():
    agg = _aggregator(window_seconds=60.0)
    for i in range(3):
        agg.try_aggregate(_event(EventType.LIKE, name=f"u{i}"), Priority.P4)

    agg.clear()
    assert agg.pending_buckets() == 0


# ---------------------------------------------------------------------------
# Aggregate event invariants
# ---------------------------------------------------------------------------

def test_aggregated_event_has_window_timestamps():
    """AggregatedEvent tem window_start e window_end timezone-aware."""
    agg = _aggregator(window_seconds=60.0, max_bucket_size=2)

    agg.try_aggregate(_event(EventType.LIKE, name="u1"), Priority.P4)
    result = agg.try_aggregate(_event(EventType.LIKE, name="u2"), Priority.P4)

    assert isinstance(result, AggregatedEvent)
    assert result.window_start.tzinfo is not None
    assert result.window_end.tzinfo is not None
    assert result.window_end >= result.window_start


def test_aggregated_event_representative_payload_is_preserved():
    """AggregatedEvent preserva o payload do primeiro evento da janela."""
    agg = _aggregator(window_seconds=60.0, max_bucket_size=2)

    payload_first = {"count": 5}
    payload_second = {"count": 99}

    agg.try_aggregate(_event(EventType.SHARE, payload=payload_first), Priority.P4)
    result = agg.try_aggregate(_event(EventType.SHARE, payload=payload_second), Priority.P4)

    assert isinstance(result, AggregatedEvent)
    assert result.representative_payload == payload_first
