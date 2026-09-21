"""Injeção de falhas e validação de observabilidade (Fase 7)."""

import asyncio
import pytest

from src.engine.config import EngineConfig
from src.engine.metrics import EngineMetrics
from src.engine.dispatcher import Dispatcher, NullConsumer
from src.engine.processor import EventProcessor
from src.observability.health import Watchdog, ComponentState, HealthStatus
from src.observability.audit import EventAuditLogger
from src.domain.events import Event, EventUser, EventType
from src.domain.priorities import Priority
from datetime import datetime, timezone


@pytest.mark.asyncio
async def test_failure_injection_processor_stall():
    """Valida se o watchdog detecta que o processor travou."""

    # Timeout extremamente baixo pro teste ser rápido
    watchdog = Watchdog(timeout_seconds=0.2)
    metrics = EngineMetrics()
    dispatcher = Dispatcher(metrics)
    dispatcher.register(NullConsumer())

    # Criamos o processor acoplado ao watchdog
    processor = EventProcessor(
        config=EngineConfig(n_workers=1),
        metrics=metrics,
        dispatcher=dispatcher,
        watchdog=watchdog
    )

    await processor.start()

    # Verifica status saudável inicial (após processar algo)
    event = Event(
        event_id="1",
        event_type=EventType.GIFT,
        source="test",
        timestamp=datetime.now(timezone.utc),
        user=EventUser(external_id="user", display_name="user"),
        payload={"gift_id": "1"}
    )
    await processor.receive(event)
    await asyncio.sleep(0.05) # Dá tempo de processar

    snap = watchdog.check()
    print("DEBUG SNAPSHOT 1:", snap)
    assert snap["all_healthy"] is True
    assert snap["components"]["EventProcessor"]["status"] == "healthy"
    assert snap["components"]["EventProcessor"]["state"] == "READY"

    # Simulamos um stall (travamento silencioso) no worker
    # Injetamos um mock no dispatcher que faz sleep longo sem disparar error
    class StallingConsumer(NullConsumer):
        @property
        def name(self): return "staller"
        async def handle(self, ev):
            await asyncio.sleep(0.5) # Maior que o watchdog.timeout

    dispatcher.register(StallingConsumer())

    # Dispara novo evento que vai travar o worker
    await processor.receive(event)

    # Espera um pouco, o watchdog deve disparar
    await asyncio.sleep(0.3)

    snap2 = watchdog.check()
    assert snap2["all_healthy"] is False
    assert snap2["components"]["EventProcessor"]["status"] == "unhealthy"
    assert snap2["components"]["EventProcessor"]["state"] == "DEGRADED"

    await processor.stop()
