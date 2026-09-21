"""Testes do RobloxBridge: tradução, buffer, cursor, ack, eviction."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.adapters.roblox import RobloxBridge, RobloxBridgeConfig, to_envelope
from src.domain.events import AggregatedEvent, Event, EventType, EventUser
from src.domain.priorities import Priority


def _event(event_type: EventType = EventType.COMMENT, **kwargs) -> Event:
    defaults: dict = {
        "event_type": event_type,
        "source": "tiktok",
        "user": EventUser(display_name="joao", external_id="tiktok:123"),
        "payload": {"text": "oi"},
    }
    defaults.update(kwargs)
    return Event(**defaults)


def _aggregated(**kwargs) -> AggregatedEvent:
    now = datetime.now(timezone.utc)
    defaults: dict = {
        "event_type": EventType.LIKE,
        "count": 5,
        "window_start": now - timedelta(seconds=2),
        "window_end": now,
        "source": "tiktok",
        "representative_payload": {"count": 1},
        "priority": Priority.P4,
    }
    defaults.update(kwargs)
    return AggregatedEvent(**defaults)


# ---------------------------------------------------------------------------
# to_envelope
# ---------------------------------------------------------------------------


def test_to_envelope_from_event_preserves_identity_and_resolves_priority():
    event = _event(event_type=EventType.GIFT, payload={"gift_name": "rose"})
    envelope = to_envelope(event, sequence_number=1)

    assert envelope.event_id == event.event_id
    assert envelope.event_type == "GIFT"
    assert envelope.priority == int(Priority.P1)  # baseline de GIFT
    assert envelope.sequence_number == 1
    assert envelope.user == {"display_name": "joao", "external_id": "tiktok:123"}


def test_to_envelope_from_aggregated_event_uses_aggregate_id():
    aggregated = _aggregated()
    envelope = to_envelope(aggregated, sequence_number=7)

    assert envelope.event_id == aggregated.aggregate_id
    assert envelope.event_type == "AGGREGATED"
    assert envelope.payload["count"] == 5
    assert envelope.priority == int(Priority.P4)


def test_envelope_to_dict_is_json_serializable_shape():
    event = _event()
    envelope = to_envelope(event, sequence_number=1)
    d = envelope.to_dict()

    assert d["schema_version"] == "1.0"
    assert set(d.keys()) == {
        "schema_version",
        "sequence_number",
        "event_id",
        "event_type",
        "timestamp",
        "priority",
        "payload",
        "source",
        "user",
    }


# ---------------------------------------------------------------------------
# RobloxBridge — consumer + buffer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_handle_appends_and_is_retrievable():
    bridge = RobloxBridge(RobloxBridgeConfig(buffer_capacity=10))
    await bridge.handle(_event())

    envelopes, cursor, gap = bridge.get_events_since(since=0)

    assert len(envelopes) == 1
    assert cursor == 1
    assert gap is False


@pytest.mark.asyncio
async def test_bridge_get_events_since_only_returns_newer_events():
    bridge = RobloxBridge(RobloxBridgeConfig(buffer_capacity=10))
    for _ in range(3):
        await bridge.handle(_event())

    envelopes, cursor, _ = bridge.get_events_since(since=2)

    assert len(envelopes) == 1
    assert envelopes[0].sequence_number == 3
    assert cursor == 3


@pytest.mark.asyncio
async def test_bridge_does_not_delete_events_on_read():
    bridge = RobloxBridge(RobloxBridgeConfig(buffer_capacity=10))
    await bridge.handle(_event())

    first_read, _, _ = bridge.get_events_since(since=0)
    second_read, _, _ = bridge.get_events_since(since=0)

    assert first_read == second_read
    assert len(second_read) == 1


@pytest.mark.asyncio
async def test_bridge_respects_limit_and_max_events_per_poll():
    bridge = RobloxBridge(
        RobloxBridgeConfig(buffer_capacity=50, max_events_per_poll=5, default_events_per_poll=5)
    )
    for _ in range(10):
        await bridge.handle(_event())

    envelopes, _, _ = bridge.get_events_since(since=0, limit=100)  # pede mais que o teto

    assert len(envelopes) == 5  # teto do config vence o limit pedido


@pytest.mark.asyncio
async def test_bridge_evicts_oldest_when_buffer_full():
    bridge = RobloxBridge(RobloxBridgeConfig(buffer_capacity=3))
    for _ in range(5):
        await bridge.handle(_event())

    envelopes, cursor, _ = bridge.get_events_since(since=0, limit=100)

    assert cursor == 5
    assert [e.sequence_number for e in envelopes] == [3, 4, 5]
    assert bridge.health_snapshot()["events_evicted_total"] == 2


@pytest.mark.asyncio
async def test_bridge_detects_gap_when_since_points_to_evicted_range():
    bridge = RobloxBridge(RobloxBridgeConfig(buffer_capacity=3))
    for _ in range(5):
        await bridge.handle(_event())

    # since=1 mas só 3,4,5 sobrevivem no buffer -> houve gap
    _, _, gap = bridge.get_events_since(since=1)

    assert gap is True


@pytest.mark.asyncio
async def test_bridge_no_gap_when_since_matches_oldest_available_minus_one():
    bridge = RobloxBridge(RobloxBridgeConfig(buffer_capacity=3))
    for _ in range(3):
        await bridge.handle(_event())

    _, _, gap = bridge.get_events_since(since=0)

    assert gap is False


def test_bridge_ack_advances_but_never_regresses():
    bridge = RobloxBridge()

    assert bridge.ack(up_to_sequence=10) == 10
    assert bridge.ack(up_to_sequence=3) == 10  # não regride
    assert bridge.ack(up_to_sequence=15) == 15


def test_bridge_can_handle_is_always_true_in_this_phase():
    bridge = RobloxBridge()
    assert bridge.can_handle(_event()) is True
    assert bridge.can_handle(_aggregated()) is True


def test_bridge_name_matches_dispatcher_protocol():
    bridge = RobloxBridge()
    assert bridge.name == "roblox_bridge"


@pytest.mark.asyncio
async def test_bridge_health_snapshot_reflects_state():
    bridge = RobloxBridge(RobloxBridgeConfig(buffer_capacity=10))
    await bridge.handle(_event())
    bridge.get_events_since(since=0)
    bridge.ack(up_to_sequence=1)

    snapshot = bridge.health_snapshot()

    assert snapshot["buffer_depth"] == 1
    assert snapshot["events_delivered_total"] == 1
    assert snapshot["last_acknowledged_sequence"] == 1
    assert snapshot["last_event_at"] is not None
    assert snapshot["last_poll_at"] is not None


def test_config_rejects_invalid_values():
    with pytest.raises(ValueError):
        RobloxBridgeConfig(buffer_capacity=0)
    with pytest.raises(ValueError):
        RobloxBridgeConfig(max_events_per_poll=0)
    with pytest.raises(ValueError):
        RobloxBridgeConfig(default_events_per_poll=1000, max_events_per_poll=10)
