"""Adapter entre Dispatcher, InteractionRuleEngine e RobloxBridge."""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.domain.events import AggregatedEvent, Event
from src.interaction.engine import InteractionRuleEngine

if TYPE_CHECKING:
    from src.adapters.roblox import RobloxBridge


class InteractionConsumer:
    """Consumer isolado: falha de uma ação não derruba os demais consumers."""

    def __init__(self, engine: InteractionRuleEngine, bridge: RobloxBridge) -> None:
        self.engine = engine
        self.bridge = bridge

    @property
    def name(self) -> str:
        return "interaction_rules"

    def can_handle(self, event: Event | AggregatedEvent) -> bool:
        return isinstance(event, Event)

    async def handle(self, event: Event | AggregatedEvent) -> None:
        if not isinstance(event, Event):
            return
        game_events = self.engine.evaluate(event)
        for game_event in game_events:
            if not self.engine.is_expired(game_event):
                await self.bridge.publish_game_event(game_event)
