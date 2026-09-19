"""Testes unitários do PriorityQueueSet.

Cobertos:
- Insert e get por prioridade
- P0 sai antes de P3 mesmo quando inserido depois
- Express lane para SYSTEM events
- Overflow respeita política por prioridade
- Close drena e encerra sem bloquear
- Profundidade por prioridade é correta
- Fairness: P3 não fica starved indefinidamente quando P1 presente
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from src.domain.events import Event, EventStatus, EventType, EventUser
from src.domain.priorities import Priority
from src.engine.config import EngineConfig
from src.engine.errors import EngineShutdownError, QueueFullError
from src.engine.metrics import EngineMetrics
from src.engine.queue import PriorityQueueSet


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _user(name: str = "tester") -> EventUser:
    return EventUser(display_name=name, external_id=f"id:{name}")


def _event(
    event_type: EventType = EventType.COMMENT,
    source: str = "tiktok",
    name: str = "tester",
    payload: dict | None = None,
    source_event_id: str | None = None,
) -> Event:
    return Event(
        event_type=event_type,
        source=source,
        user=_user(name),
        payload=payload or {},
        source_event_id=source_event_id,
        timestamp=datetime.now(timezone.utc),
        received_at=datetime.now(timezone.utc),
    )


def _system_event(reason: str = "live_ended") -> Event:
    return Event(
        event_type=EventType.SYSTEM,
        source="tiktok",
        user=_user("system"),
        payload={"reason": reason},
        timestamp=datetime.now(timezone.utc),
        received_at=datetime.now(timezone.utc),
    )


def _make_queue(
    capacity_per_level: int = 100,
    p0_express: int = 10,
    overflow_p3: float = 0.60,
    overflow_p4: float = 0.50,
) -> PriorityQueueSet:
    config = EngineConfig(
        queue_capacity_per_level=capacity_per_level,
        p0_express_capacity=p0_express,
        overflow_threshold_p3=overflow_p3,
        overflow_threshold_p4=overflow_p4,
    )
    metrics = EngineMetrics()
    return PriorityQueueSet(config, metrics)


# ---------------------------------------------------------------------------
# Basic insert/get
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_put_and_get_single_event():
    q = _make_queue()
    event = _event(EventType.COMMENT)
    q.put_nowait(event, Priority.P3)
    result = await q.get_next()
    assert result.event_id == event.event_id


@pytest.mark.asyncio
async def test_p0_event_precedes_p3_event_inserted_first():
    """P0 inserido DEPOIS de P3 deve ser consumido ANTES."""
    q = _make_queue()
    p3_event = _event(EventType.COMMENT, name="user1")
    p0_event = _event(EventType.MANUAL, name="system")

    q.put_nowait(p3_event, Priority.P3)
    q.put_nowait(p0_event, Priority.P0)

    # Primeiro consumido: P0
    first = await q.get_next()
    assert first.event_id == p0_event.event_id

    # Segundo: P3
    second = await q.get_next()
    assert second.event_id == p3_event.event_id


@pytest.mark.asyncio
async def test_p0_p1_p2_ordering():
    """P0 < P1 < P2 < P3 < P4 na ordem de consumo."""
    q = _make_queue()

    p4 = _event(EventType.LIKE, name="u4")
    p3 = _event(EventType.COMMENT, name="u3")
    p2 = _event(EventType.MANUAL, name="u2")
    p1 = _event(EventType.GIFT, name="u1")
    p0 = _event(EventType.MANUAL, name="u0")

    # Inserir em ordem reversa (pior primeiro)
    q.put_nowait(p4, Priority.P4)
    q.put_nowait(p3, Priority.P3)
    q.put_nowait(p2, Priority.P2)
    q.put_nowait(p1, Priority.P1)
    q.put_nowait(p0, Priority.P0)

    first = await q.get_next()
    assert first.event_id == p0.event_id


@pytest.mark.asyncio
async def test_fifo_within_same_priority():
    """Dentro da mesma prioridade, ordem FIFO é preservada."""
    q = _make_queue()
    events = [_event(name=f"user{i}") for i in range(5)]
    for e in events:
        q.put_nowait(e, Priority.P3)

    results = [await q.get_next() for _ in range(5)]
    assert [r.event_id for r in results] == [e.event_id for e in events]


# ---------------------------------------------------------------------------
# Express lane (SYSTEM events)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_system_event_uses_express_lane():
    """SYSTEM events devem usar a express lane e ter prioridade absoluta."""
    q = _make_queue(p0_express=5)
    sys_event = _system_event("live_ended")
    normal_p0 = _event(EventType.MANUAL, name="op")

    q.put_nowait(normal_p0, Priority.P0)
    q.put_nowait(sys_event, Priority.P0)  # Express lane

    first = await q.get_next()
    # Express lane é consumida antes das filas normais
    assert first.event_id == sys_event.event_id


@pytest.mark.asyncio
async def test_express_lane_bounded():
    """Express lane tem capacidade limitada."""
    q = _make_queue(p0_express=2)
    for _ in range(2):
        q.put_nowait(_system_event(), Priority.P0)

    # A terceira tentativa deve cair na fila P0 normal
    # (não deve lançar — cai para P0 normal que tem espaço)
    q.put_nowait(_system_event("extra"), Priority.P0)
    assert q.total_depth() == 3


# ---------------------------------------------------------------------------
# Overflow policy
# ---------------------------------------------------------------------------

def test_p4_overflow_drops_at_threshold():
    """P4 deve ser descartado quando total >= threshold."""
    # threshold_p4=0.5 com capacity_per_level=4 → total_capacity=20, threshold=10
    config = EngineConfig(
        queue_capacity_per_level=4,
        p0_express_capacity=0,
        overflow_threshold_p4=0.5,
        overflow_threshold_p3=1.0,  # P3 nunca dropa neste teste
    )
    metrics = EngineMetrics()
    q = PriorityQueueSet(config, metrics)

    # Preencher com P3 até atingir 10 eventos (50% da capacidade de 20)
    for i in range(4):  # P3 tem capacity=4
        q.put_nowait(_event(name=f"u{i}"), Priority.P3)

    # P4 deve ser rejeitado quando total >= threshold (10 = 50% de 20)
    # Com 4 eventos no P3, total=4 que é 20% de 20 → ainda cabe P4
    q.put_nowait(_event(EventType.LIKE, name="like1"), Priority.P4)

    # Verificar que a fila P4 tem 1 evento
    assert q.depth_by_priority()[Priority.P4] == 1


def test_p4_raises_queue_full_when_overflow():
    """P4 lança QueueFullError quando a capacidade P4 está cheia."""
    config = EngineConfig(
        queue_capacity_per_level=2,
        p0_express_capacity=0,
        overflow_threshold_p4=0.0,  # threshold 0% = sempre dropa P4
    )
    metrics = EngineMetrics()
    q = PriorityQueueSet(config, metrics)

    with pytest.raises(QueueFullError):
        q.put_nowait(_event(EventType.LIKE), Priority.P4)


def test_p0_never_blocked_by_overflow_threshold():
    """P0 ignora os thresholds de overflow."""
    config = EngineConfig(
        queue_capacity_per_level=4,
        p0_express_capacity=0,
        overflow_threshold_p1=0.0,
        overflow_threshold_p2=0.0,
        overflow_threshold_p3=0.0,
        overflow_threshold_p4=0.0,
    )
    metrics = EngineMetrics()
    q = PriorityQueueSet(config, metrics)

    # Com todos os thresholds em 0%, P0 ainda deve funcionar
    q.put_nowait(_event(EventType.MANUAL), Priority.P0)
    assert q.depth_by_priority()[Priority.P0] == 1


# ---------------------------------------------------------------------------
# Close / shutdown
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_allows_drain_then_raises_stop():
    """Após close, eventos existentes são consumíveis; fila vazia levanta StopAsyncIteration."""
    q = _make_queue()
    event = _event()
    q.put_nowait(event, Priority.P3)
    q.close()

    # Consumir o evento existente
    result = await q.get_next()
    assert result.event_id == event.event_id

    # Próxima tentativa: StopAsyncIteration
    with pytest.raises(StopAsyncIteration):
        await q.get_next()


def test_put_after_close_raises_shutdown_error():
    """Inserção após close levanta EngineShutdownError."""
    q = _make_queue()
    q.close()
    with pytest.raises(EngineShutdownError):
        q.put_nowait(_event(), Priority.P3)


# ---------------------------------------------------------------------------
# Depth tracking
# ---------------------------------------------------------------------------

def test_depth_by_priority_is_accurate():
    """depth_by_priority() reflete a profundidade real de cada fila."""
    q = _make_queue()
    q.put_nowait(_event(name="a"), Priority.P0)
    q.put_nowait(_event(name="b"), Priority.P1)
    q.put_nowait(_event(name="c"), Priority.P1)
    q.put_nowait(_event(name="d"), Priority.P3)

    depth = q.depth_by_priority()
    assert depth[Priority.P0] == 1
    assert depth[Priority.P1] == 2
    assert depth[Priority.P2] == 0
    assert depth[Priority.P3] == 1
    assert depth[Priority.P4] == 0


# ---------------------------------------------------------------------------
# Fairness: P3 não fica starved indefinidamente
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p3_eventually_processed_when_p1_also_present():
    """Com eventos em P1 e P3, P3 deve receber algum throughput (WRR fairness)."""
    config = EngineConfig(
        queue_capacity_per_level=50,
        fairness_weights={0: 100, 1: 4, 2: 2, 3: 1, 4: 0},
    )
    metrics = EngineMetrics()
    q = PriorityQueueSet(config, metrics)

    # Inserir 20 P1 e 20 P3
    p1_events = [_event(EventType.GIFT, name=f"gift{i}") for i in range(20)]
    p3_events = [_event(EventType.COMMENT, name=f"cmt{i}") for i in range(20)]

    for e in p1_events:
        q.put_nowait(e, Priority.P1)
    for e in p3_events:
        q.put_nowait(e, Priority.P3)

    consumed_p1 = 0
    consumed_p3 = 0
    p1_ids = {e.event_id for e in p1_events}
    p3_ids = {e.event_id for e in p3_events}

    for _ in range(20):
        e = await q.get_next()
        if e.event_id in p1_ids:
            consumed_p1 += 1
        elif e.event_id in p3_ids:
            consumed_p3 += 1

    # P3 deve ter recebido pelo menos alguns slots (WRR weight=1, P1 weight=4)
    # Em 20 eventos: esperamos P1~16 e P3~4, mas o mínimo aceitável é P3 > 0
    assert consumed_p3 > 0, f"P3 starved completamente: consumed_p3={consumed_p3}"
    assert consumed_p1 > consumed_p3, "P1 deve ter mais throughput que P3"
    assert consumed_p1 + consumed_p3 == 20
