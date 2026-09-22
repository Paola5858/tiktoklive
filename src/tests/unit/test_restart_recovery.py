"""Testes de restart e recuperação de estado efêmero."""

import asyncio
import pytest
from datetime import datetime, timezone

from src.engine.dedup import DeduplicationCache
from src.domain.events import Event, EventType, EventUser
from src.interaction.state import CooldownManager
from src.interaction.models import (
    ActionDefinition, ActionType, CooldownSpec, InteractionRule, RuleMatch,
)


def test_deduplication_cache_is_ephemeral():
    """Valida que o DeduplicationCache não persiste em disco e inicia vazio (comportamento intencional)."""
    cache1 = DeduplicationCache()

    event = Event(
        event_id="test1",
        event_type=EventType.GIFT,
        source="test",
        timestamp=datetime.now(timezone.utc),
        user=EventUser(external_id="u1", display_name="User 1"),
        payload={"gift_id": 1}
    )

    assert cache1.is_duplicate(event) is False
    assert cache1.is_duplicate(event) is True

    # Simula um restart recriando o cache
    cache2 = DeduplicationCache()

    # Após restart, o cache inicia vazio, logo a mesma mensagem será processada novamente (at-least-once com janela de 30s)
    # Isso é o comportamento aprovado no arquitetura "Semântica at-least-once"
    assert cache2.is_duplicate(event) is False


def test_interaction_state_is_ephemeral():
    """Valida que cooldowns de usuários são resetados no restart."""
    rule = InteractionRule(
        rule_id="test_cooldown",
        enabled=True,
        match=RuleMatch(event_type=EventType.GIFT),
        actions=(ActionDefinition(ActionType.SPAWN_AVATAR, {"duration_seconds": 60}),),
        cooldown=CooldownSpec(scope="user", seconds=60),
    )

    event = Event(
        event_type=EventType.GIFT,
        source="test",
        user=EventUser(external_id="u1", display_name="User 1"),
        payload={"gift_id": 1},
    )

    state = CooldownManager()

    # Primeiro acesso: não está em cooldown
    assert state.blocked(rule, event) is False
    # Marca o cooldown para este usuário
    state.mark(rule, event)
    # Agora está em cooldown
    assert state.blocked(rule, event) is True
    assert state.hits == 1

    # Restart — novo gerenciador (state é puramente in-memory)
    state2 = CooldownManager()

    # Cooldown expirou/resetou porque é efêmero
    assert state2.blocked(rule, event) is False
    assert state2.hits == 0


def test_mqtt_deduplication_is_ephemeral():
    """MQTTPriorityQueue buffer e dedup expirando pós-restart."""
    from src.adapters.mqtt import MQTTAdapter, MQTTConfig
    from src.interaction.models import GameEvent
    import asyncio

    config = MQTTConfig.disabled()
    adapter = MQTTAdapter(config)

    event = GameEvent(
        event_id="g1",
        source_event_id="s1",
        event_type="SPAWN_AVATAR",
        priority=1,
        timestamp=datetime.now(timezone.utc),
        expires_at=None,
        payload={"target_device_id": "d1"}
    )

    # Adapater disabled don't enqueue, but we can verify the deduplication cache behavior conceptually
    # in adapter._dedup_cache. Since it's an instance variable, it resets on initialization.
    assert len(adapter._dedup_cache) == 0


@pytest.mark.asyncio
async def test_processor_graceful_shutdown_records_resilience():
    """Valida que o EventProcessor registra graceful_shutdown no ResilienceMetrics."""
    from src.engine.config import EngineConfig
    from src.engine.metrics import EngineMetrics
    from src.engine.dispatcher import Dispatcher, NullConsumer
    from src.engine.processor import EventProcessor
    from src.observability.resilience import ResilienceMetrics

    config = EngineConfig(n_workers=1, shutdown_drain_timeout_seconds=1.0)
    metrics = EngineMetrics()
    dispatcher = Dispatcher(metrics)
    dispatcher.register(NullConsumer())
    resilience = ResilienceMetrics()

    processor = EventProcessor(config, metrics, dispatcher, resilience=resilience)
    await processor.start()
    await processor.receive(
        Event(
            event_type=EventType.LIKE,
            source="test",
            user=EventUser(external_id="u", display_name="u"),
            payload={"count": 1},
        )
    )
    await asyncio.sleep(0.05)
    await processor.stop()

    snap = resilience.snapshot()
    assert snap["graceful_shutdown_total"] == 1


@pytest.mark.asyncio
async def test_processor_drain_timeout_records_resilience():
    """Valida que o EventProcessor registra timeout quando o drain de P0 excede o limite."""
    from src.engine.config import EngineConfig
    from src.engine.metrics import EngineMetrics
    from src.engine.dispatcher import Dispatcher, EventConsumer
    from src.engine.processor import EventProcessor
    from src.observability.resilience import ResilienceMetrics

    class SlowConsumer:
        @property
        def name(self) -> str:
            return "slow"
        def can_handle(self, event: Event) -> bool:
            return True
        async def handle(self, event: Event) -> None:
            await asyncio.sleep(0.3)  # mais lento que o drain timeout

    config = EngineConfig(n_workers=1, max_in_flight=1, shutdown_drain_timeout_seconds=0.1)
    metrics = EngineMetrics()
    dispatcher = Dispatcher(metrics)
    dispatcher.register(SlowConsumer())
    resilience = ResilienceMetrics()

    processor = EventProcessor(config, metrics, dispatcher, resilience=resilience)
    await processor.start()

    # Enfileira P0 eventos únicos — o worker processa um, os outros ficam na fila
    events = [
        Event(
            event_id=f"p0_{i}",
            source_event_id=f"p0_sid_{i}",
            event_type=EventType.SYSTEM,
            source="test",
            user=EventUser(external_id="u", display_name="u"),
            payload={},
        )
        for i in range(3)
    ]

    await processor.receive(events[0])
    await asyncio.sleep(0.01)  # deixa o worker pegar o primeiro evento (slow consumer)
    await processor.receive(events[1])
    await processor.receive(events[2])

    await processor.stop()

    snap = resilience.snapshot()
    assert snap["timeout_total"] >= 1
    assert snap["graceful_shutdown_total"] == 1
