"""Adapter entre Dispatcher, InteractionRuleEngine e RobloxBridge."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from src.domain.events import AggregatedEvent, Event
from src.interaction.engine import InteractionRuleEngine

if TYPE_CHECKING:
    from src.adapters.roblox import RobloxBridge

LOGGER = logging.getLogger(__name__)


class InteractionConsumer:
    """Consumer isolado: falha de uma ação não derruba os demais consumers."""

    def __init__(
        self, engine: InteractionRuleEngine, bridges: Any | list[Any] | None = None
    ) -> None:
        self.engine = engine
        if isinstance(bridges, list):
            self.bridges = list(bridges)
        elif bridges is not None:
            self.bridges = [bridges]
        else:
            self.bridges = []

    @property
    def bridge(self) -> Any | None:
        """Compatibilidade retroativa com código que espera self.bridge único."""
        return self.bridges[0] if self.bridges else None

    @property
    def name(self) -> str:
        return "interaction_rules"

    def can_handle(self, event: Event | AggregatedEvent) -> bool:
        return isinstance(event, Event)

    async def handle(self, event: Event | AggregatedEvent) -> None:
        if not isinstance(event, Event):
            return
        try:
            game_events = self.engine.evaluate(event)
        except Exception:
            LOGGER.exception("InteractionRuleEngine falhou ao avaliar evento %s", event.event_id)
            return
        for game_event in game_events:
            if self.engine.is_expired(game_event):
                continue
            for bridge in self.bridges:
                if hasattr(bridge, "publish_game_event"):
                    try:
                        await bridge.publish_game_event(game_event)
                    except Exception:
                        LOGGER.exception(
                            "Bridge %s falhou ao publicar game_event %s",
                            bridge.__class__.__name__,
                            game_event.event_id,
                        )
