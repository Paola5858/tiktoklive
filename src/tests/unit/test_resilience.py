import asyncio
import time
import pytest
from unittest.mock import Mock

from src.observability.resilience import ResilienceMetrics
from src.observability.health import Watchdog, ComponentState, HealthStatus
from src.adapters.mqtt import MQTTPriorityQueue, MQTTMessage
from src.adapters.obs import OBSPriorityQueue, OBSAction, OBSActionType


def test_resilience_metrics():
    metrics = ResilienceMetrics()
    metrics.record_retry(exhausted=False)
    metrics.record_retry(exhausted=True)
    metrics.record_recovery(success=True)
    metrics.record_recovery(success=False)
    metrics.record_watchdog_trigger()
    metrics.record_timeout()

    snap = metrics.snapshot()
    assert snap["retry_total"] == 2
    assert snap["retry_exhausted_total"] == 1
    assert snap["recovery_attempts_total"] == 2
    assert snap["recovery_success_total"] == 1
    assert snap["recovery_failure_total"] == 1
    assert snap["watchdog_trigger_total"] == 1
    assert snap["timeout_total"] == 1
    assert snap["worker_restart_total"] == 0
    assert snap["graceful_shutdown_total"] == 0
    assert snap["unclean_shutdown_total"] == 0
    assert snap["event_expired_total"] == 0


def test_resilience_metrics_shutdown_and_restart():
    """Valida os novos métodos record_graceful_shutdown, record_unclean_shutdown,
    record_worker_restart e record_event_expired."""
    metrics = ResilienceMetrics()
    metrics.record_graceful_shutdown()
    metrics.record_unclean_shutdown()
    metrics.record_worker_restart()
    metrics.record_event_expired()

    snap = metrics.snapshot()
    assert snap["graceful_shutdown_total"] == 1
    assert snap["unclean_shutdown_total"] == 1
    assert snap["worker_restart_total"] == 1
    assert snap["event_expired_total"] == 1


@pytest.mark.asyncio
async def test_watchdog_records_resilience_trigger():
    """Valida que o Watchdog registra disparos no ResilienceMetrics compartilhado."""
    resilience = ResilienceMetrics()
    watchdog = Watchdog(timeout_seconds=0.1, resilience=resilience)
    c1 = watchdog.register("c1")
    c1.mark_active()

    assert watchdog.check()["all_healthy"] is True
    assert resilience.snapshot()["watchdog_trigger_total"] == 0

    await asyncio.sleep(0.15)
    watchdog.check()

    assert resilience.snapshot()["watchdog_trigger_total"] == 1


@pytest.mark.asyncio
async def test_watchdog_trigger_counter():
    watchdog = Watchdog(timeout_seconds=0.1)
    c1 = watchdog.register("c1")
    c1.mark_active()

    assert watchdog.check()["all_healthy"] is True
    assert watchdog.check()["watchdog_trigger_total"] == 0

    await asyncio.sleep(0.15)
    snap = watchdog.check()
    assert snap["all_healthy"] is False
    assert snap["watchdog_trigger_total"] == 1


@pytest.mark.asyncio
async def test_mqtt_queue_shutdown():
    queue = MQTTPriorityQueue(maxsize_per_level=10)

    # Test gracefully stopping empty queue
    queue.close()

    with pytest.raises(StopAsyncIteration):
        await queue.get_next()


@pytest.mark.asyncio
async def test_obs_queue_shutdown():
    queue = OBSPriorityQueue(maxsize_per_level=10)

    # Test gracefully stopping empty queue
    queue.close()

    with pytest.raises(StopAsyncIteration):
        await queue.get_next()


@pytest.mark.asyncio
async def test_mqtt_message_ttl_monotonic():
    now_mono = time.monotonic()
    msg = MQTTMessage(
        topic="test",
        payload=b"test",
        qos=0,
        priority=1,
        created_at=now_mono,
        expires_at=now_mono + 0.1
    )

    assert msg.is_expired() is False
    await asyncio.sleep(0.15)
    assert msg.is_expired() is True
