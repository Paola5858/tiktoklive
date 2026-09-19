"""Testes de fundação do domínio: modelo de prioridade."""

from __future__ import annotations

from src.domain.events import EventType
from src.domain.priorities import Priority, resolve_priority


def test_system_events_get_highest_priority():
    assert resolve_priority(EventType.SYSTEM) == Priority.P0


def test_gifts_outrank_comments():
    assert resolve_priority(EventType.GIFT) < resolve_priority(EventType.COMMENT)


def test_priority_ordering_matches_intended_urgency():
    assert sorted(Priority) == [Priority.P0, Priority.P1, Priority.P2, Priority.P3, Priority.P4]


def test_every_event_type_has_a_default_priority():
    for event_type in EventType:
        resolve_priority(event_type)  # não deve levantar KeyError
