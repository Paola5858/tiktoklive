"""Testes da camada de observabilidade (Fase 7)."""

import asyncio
from datetime import datetime
import json
import logging
from pathlib import Path
import tempfile
import time

import pytest

from src.observability.health import ComponentState, HealthStatus, Watchdog
from src.observability.audit import EventAuditLogger
from src.observability.metrics import OperationalSnapshot
from src.engine.metrics import EngineMetrics
from src.logging import setup_logging, JsonFormatter


def test_watchdog_detects_stall():
    watchdog = Watchdog(timeout_seconds=0.1)

    comp1 = watchdog.register("TestComp")
    assert comp1.state == ComponentState.STARTING
    assert comp1.status == HealthStatus.UNKNOWN

    comp1.mark_active()
    assert comp1.state == ComponentState.READY
    assert comp1.status == HealthStatus.UNKNOWN # Nenhuma falha/sucesso registrado ainda

    comp1.record_success()
    assert comp1.status == HealthStatus.HEALTHY

    snap = watchdog.check()
    assert snap["all_healthy"] is True
    assert "TestComp" not in snap["degraded_components"]

    # Simula travamento (tempo > 0.1s)
    time.sleep(0.15)

    snap2 = watchdog.check()
    assert snap2["all_healthy"] is False
    assert "TestComp" in snap2["degraded_components"]

    assert comp1.state == ComponentState.DEGRADED
    assert comp1.status == HealthStatus.UNHEALTHY
    assert "Watchdog timeout" in comp1.failure_reason

    # Se ele recuperar, fica saudável
    comp1.record_success()
    assert comp1.state == ComponentState.READY
    assert comp1.status == HealthStatus.HEALTHY

def test_watchdog_ignores_stopped():
    watchdog = Watchdog(timeout_seconds=0.1)
    comp = watchdog.register("StoppedComp")
    comp.state = ComponentState.STOPPED

    time.sleep(0.15)
    snap = watchdog.check()
    assert snap["all_healthy"] is True
    assert comp.status != HealthStatus.UNHEALTHY

@pytest.mark.asyncio
async def test_audit_logger_writes_jsonl():
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = EventAuditLogger(log_dir=tmpdir, max_queue_size=10)
        await logger.start()

        logger.log_event({"event_id": "123", "status": "processed"})
        logger.log_event({"event_id": "456", "status": "failed"})

        await logger.stop()

        # Verificar arquivo gerado
        log_dir = Path(tmpdir)
        files = list(log_dir.glob("*.jsonl"))
        assert len(files) == 1

        content = files[0].read_text(encoding="utf-8").strip().split("\n")
        assert len(content) == 2

        data1 = json.loads(content[0])
        assert data1["event_id"] == "123"
        assert data1["status"] == "processed"

        data2 = json.loads(content[1])
        assert data2["event_id"] == "456"

def test_operational_snapshot():
    metrics = EngineMetrics()
    metrics.record_received()

    watchdog = Watchdog()
    comp = watchdog.register("Test")
    comp.record_success()

    snapshot_manager = OperationalSnapshot(metrics, watchdog)
    snap = snapshot_manager.get_snapshot()

    assert snap["status"] == "healthy"
    assert snap["throughput"]["received"] == 1
    assert "Test" in snap["health"]
    assert snap["health"]["Test"]["status"] == "healthy"

def test_json_formatter():
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test_logger", level=logging.INFO, pathname="",
        lineno=0, msg="Test %s", args=("message",), exc_info=None
    )

    # Injetar `event_id` via extra
    record.__dict__["event_id"] = "ev-123"

    formatted = formatter.format(record)
    data = json.loads(formatted)

    assert data["component"] == "test_logger"
    assert data["level"] == "INFO"
    assert data["message"] == "Test message"
    assert data["event_id"] == "ev-123"
    assert "timestamp" in data
