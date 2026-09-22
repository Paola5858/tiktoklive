"""Agregação de métricas e snapshot operacional.

Reúne informações do Event Engine, conectores, regras de interação e health checks
em uma única view coerente do sistema.
"""

from __future__ import annotations

from typing import Any

from src.engine.metrics import EngineMetrics
from src.observability.health import Watchdog
from src.observability.resilience import ResilienceMetrics


class OperationalSnapshot:
    """Compila e estrutura o snapshot do sistema."""

    def __init__(
        self,
        metrics: EngineMetrics,
        watchdog: Watchdog,
        resilience: ResilienceMetrics | None = None,
    ):
        self._metrics = metrics
        self._watchdog = watchdog
        self._resilience = resilience or ResilienceMetrics()
        # Podemos registrar outros componentes (ex: regras, avatar) conforme a necessidade

    def get_snapshot(self) -> dict[str, Any]:
        """Obtém fotografia atual do sistema (útil para dashboard ou logs)."""
        engine_snap = self._metrics.snapshot()
        health_snap = self._watchdog.check()
        resilience_snap = self._resilience.snapshot()

        return {
            "status": "healthy" if health_snap["all_healthy"] else "degraded",
            "health": health_snap["components"],
            "resilience": resilience_snap,
            "queue": {
                "depth_total": engine_snap["queue_depth_total"],
                "depth_by_priority": engine_snap["queue_depth_by_priority"],
                "dropped": engine_snap["dropped"],
                "overflow": engine_snap["overflow"],
            },
            "throughput": {
                "received": engine_snap["received"],
                "accepted": engine_snap["accepted"],
                "processed": engine_snap["processed"],
                "dispatched": engine_snap["dispatched"],
                "failed": engine_snap["failed"],
                "in_flight": engine_snap["in_flight"],
            },
            "latency": engine_snap["latency"],
            # Outros subsistemas podem ser injetados aqui (ex: Roblox avatars)
        }
