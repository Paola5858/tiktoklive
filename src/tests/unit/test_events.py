"""Testes de fundação do domínio: `Event` e `EventUser`."""

from __future__ import annotations

from datetime import datetime

import pytest

from src.domain.errors import InvalidEventError
from src.domain.events import Event, EventStatus, EventType, EventUser


def _user(display_name: str = "joao", external_id: str | None = "tiktok:123") -> EventUser:
    return EventUser(display_name=display_name, external_id=external_id)


def test_event_creation_with_minimum_fields_succeeds():
    event = Event(event_type=EventType.COMMENT, source="tiktok", user=_user(), payload={"text": "oi"})

    assert event.status == EventStatus.RECEIVED
    assert event.event_id
    assert event.timestamp.tzinfo is not None


def test_two_events_get_different_ids():
    a = Event(event_type=EventType.COMMENT, source="tiktok", user=_user())
    b = Event(event_type=EventType.COMMENT, source="tiktok", user=_user())

    assert a.event_id != b.event_id


def test_event_without_source_raises():
    with pytest.raises(InvalidEventError):
        Event(event_type=EventType.COMMENT, source="", user=_user())


def test_event_with_invalid_payload_type_raises():
    with pytest.raises(InvalidEventError):
        Event(event_type=EventType.COMMENT, source="tiktok", user=_user(), payload="não é dict")  # type: ignore[arg-type]


def test_event_with_naive_timestamp_raises():
    with pytest.raises(InvalidEventError):
        Event(
            event_type=EventType.COMMENT,
            source="tiktok",
            user=_user(),
            timestamp=datetime(2026, 1, 1),  # sem timezone
        )


def test_event_user_requires_display_name():
    with pytest.raises(InvalidEventError):
        EventUser(display_name="   ")


def test_unknown_event_type_string_is_rejected_by_the_enum():
    with pytest.raises(ValueError):
        EventType("NOT_A_REAL_TYPE")


def test_deduplication_key_is_stable_for_equivalent_gift_events():
    first = Event(
        event_type=EventType.GIFT,
        source="tiktok",
        user=_user(),
        payload={"gift_name": "rose", "repeat_count": 1},
    )
    second = Event(
        event_type=EventType.GIFT,
        source="tiktok",
        user=_user(),
        payload={"gift_name": "rose", "repeat_count": 2},
    )

    assert first.deduplication_key() == second.deduplication_key()


def test_deduplication_key_differs_for_different_users():
    a = Event(
        event_type=EventType.GIFT,
        source="tiktok",
        user=_user("joao", external_id="tiktok:123"),
        payload={"gift_name": "rose"},
    )
    b = Event(
        event_type=EventType.GIFT,
        source="tiktok",
        user=_user("maria", external_id="tiktok:456"),
        payload={"gift_name": "rose"},
    )

    assert a.deduplication_key() != b.deduplication_key()
