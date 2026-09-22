"""Gerenciamento de estado e health checks dos componentes.

Permite que o sistema saiba de forma confiável se as peças
estão funcionando ou travadas, usando watchdogs e estados explícitos.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.observability.resilience import ResilienceMetrics


class ComponentState(str, Enum):
    """Estado do ciclo de vida de um componente."""
    STARTING = "STARTING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    DISCONNECTED = "DISCONNECTED"
    RECONNECTING = "RECONNECTING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class HealthStatus(str, Enum):
    """Semântica de saúde baseada em evidências."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


def now_ms() -> float:
    return time.monotonic() * 1000.0


@dataclass
class ComponentHealth:
    """Rastreador de estado e saúde de um componente."""
    name: str
    state: ComponentState = ComponentState.STARTING
    status: HealthStatus = HealthStatus.UNKNOWN
    last_success_ms: float = 0.0
    last_failure_ms: float = 0.0
    failure_reason: str | None = None
    last_activity_ms: float = field(default_factory=now_ms)

    def mark_active(self) -> None:
        """Sinaliza que o componente fez algum progresso ou está vivo."""
        self.last_activity_ms = now_ms()
        if self.state in (ComponentState.STARTING, ComponentState.RECONNECTING):
            self.state = ComponentState.READY

    def record_success(self) -> None:
        """Registra uma operação bem-sucedida (e.g. recebimento, processamento)."""
        self.last_success_ms = now_ms()
        self.mark_active()
        self.failure_reason = None
        self.status = HealthStatus.HEALTHY
        if self.state in (ComponentState.DEGRADED, ComponentState.DISCONNECTED, ComponentState.FAILED):
            self.state = ComponentState.READY

    def record_failure(self, reason: str, is_critical: bool = False) -> None:
        """Registra uma falha."""
        self.last_failure_ms = now_ms()
        self.failure_reason = reason
        self.mark_active() # Falhou, mas o processo não está travado

        if is_critical:
            self.status = HealthStatus.UNHEALTHY
            self.state = ComponentState.FAILED
        else:
            self.status = HealthStatus.DEGRADED
            if self.state == ComponentState.READY:
                self.state = ComponentState.DEGRADED

    def report_degraded(self, reason: str) -> None:
        """Alias de record_failure(is_critical=False) — para compatibilidade com callers externos."""
        self.record_failure(reason, is_critical=False)

    def snapshot(self) -> dict[str, Any]:
        """Retorna uma representação segura para a API/logs."""
        return {
            "name": self.name,
            "state": self.state.value,
            "status": self.status.value,
            "last_success_age_ms": round(now_ms() - self.last_success_ms, 1) if self.last_success_ms else None,
            "last_failure_age_ms": round(now_ms() - self.last_failure_ms, 1) if self.last_failure_ms else None,
            "last_activity_age_ms": round(now_ms() - self.last_activity_ms, 1),
            "failure_reason": self.failure_reason,
            "timestamp": time.time(),
        }


class Watchdog:
    """Monitora a atividade de componentes para detectar travamentos silenciosos."""

    def __init__(
        self,
        timeout_seconds: float = 30.0,
        resilience: "ResilienceMetrics | None" = None,
    ) -> None:
        self.timeout_ms = timeout_seconds * 1000.0
        self.components: dict[str, ComponentHealth] = {}
        self.check_triggered_count: int = 0  # watchdog_trigger_total
        self._resilience = resilience

    def register(self, name: str) -> ComponentHealth:
        health = ComponentHealth(name=name)
        self.components[name] = health
        return health

    def check(self) -> dict[str, Any]:
        """Avalia todos os componentes registrados.
        Se um componente não reportar atividade além do timeout, é marcado como UNHEALTHY/DEGRADED.
        Incrementa check_triggered_count quando algum componente é degradado.
        """
        current_time = now_ms()
        degraded_components = []

        for name, health in self.components.items():
            # Ignora componentes parados ou que já falharam definitivamente
            if health.state in (ComponentState.STOPPED, ComponentState.FAILED):
                continue

            idle_time = current_time - health.last_activity_ms
            if idle_time > self.timeout_ms:
                if health.status != HealthStatus.UNHEALTHY:
                    health.status = HealthStatus.UNHEALTHY
                    health.state = ComponentState.DEGRADED
                    health.failure_reason = f"Watchdog timeout: idle for {idle_time/1000.0:.1f}s"
                    self.check_triggered_count += 1
                    if self._resilience:
                        self._resilience.record_watchdog_trigger()
                    degraded_components.append(name)

        return {
            "all_healthy": len(degraded_components) == 0,
            "degraded_components": degraded_components,
            "components": {k: v.snapshot() for k, v in self.components.items()},
            "watchdog_trigger_total": self.check_triggered_count,
        }
