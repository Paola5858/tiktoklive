from __future__ import annotations

import pytest

from src.adapters.roblox import RobloxBridge
from src.domain.events import Event, EventType, EventUser
from src.interaction import InteractionRuleEngine, rule_from_dict


def test_game_event_reaches_existing_bridge_as_game_command():
    event = Event(
        event_type=EventType.GIFT,
        source="tiktok",
        user=EventUser("viewer", "u-1"),
        payload={"gift_id": 7, "gift_name": "fixture", "repeat_count": 1},
        event_id="source-event-1",
    )
    rule = rule_from_dict({
        "id": "fixture-gift",
        "enabled": True,
        "event_type": "GIFT",
        "match": {"gift_id": 7},
        "actions": [{"type": "SPAWN_AVATAR", "params": {"duration_seconds": 20}}],
        "priority": "P1",
    })
    engine = InteractionRuleEngine([rule])
    game_events = engine.evaluate(event)
    bridge = RobloxBridge()

    async def publish():
        for game_event in game_events:
            await bridge.publish_game_event(game_event)

    import asyncio
    asyncio.run(publish())
    envelopes, cursor, gap = bridge.get_events_since(0)
    assert cursor == 1
    assert gap is False
    assert envelopes[0].event_type == "GAME_COMMAND"
    assert envelopes[0].payload["command_type"] == "SPAWN_AVATAR"
    assert envelopes[0].payload["source_event_id"] == "source-event-1"
