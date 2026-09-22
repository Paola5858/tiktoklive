"""Métricas de resiliência e estado geral do sistema."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ResilienceMetrics:
    """Contadores para ações de recuperação, degradação e resiliência."""

    # Retries e falhas
    recovery_attempts_total: int = 0
    recovery_success_total: int = 0
    recovery_failure_total: int = 0
    retry_total: int = 0
    retry_exhausted_total: int = 0
    timeout_total: int = 0

    # Watchdog e workers
    worker_restart_total: int = 0
    watchdog_trigger_total: int = 0

    # Shutdown e expiração
    graceful_shutdown_total: int = 0
    unclean_shutdown_total: int = 0
    event_expired_total: int = 0

    def record_retry(self, exhausted: bool = False) -> None:
        """Registra tentativa de retry."""
        self.retry_total += 1
        if exhausted:
            self.retry_exhausted_total += 1

    def record_recovery(self, success: bool) -> None:
        """Registra tentativa de recuperação (reconectar, drain, fallback)."""
        self.recovery_attempts_total += 1
        if success:
            self.recovery_success_total += 1
        else:
            self.recovery_failure_total += 1

    def record_watchdog_trigger(self) -> None:
        self.watchdog_trigger_total += 1

    def record_timeout(self) -> None:
        self.timeout_total += 1

    def record_worker_restart(self) -> None:
        self.worker_restart_total += 1

    def record_graceful_shutdown(self) -> None:
        self.graceful_shutdown_total += 1

    def record_unclean_shutdown(self) -> None:
        self.unclean_shutdown_total += 1

    def record_event_expired(self) -> None:
        self.event_expired_total += 1

    def snapshot(self) -> dict[str, Any]:
        """Retorna uma representação segura para a API/logs."""
        return {
            "recovery_attempts_total": self.recovery_attempts_total,
            "recovery_success_total": self.recovery_success_total,
            "recovery_failure_total": self.recovery_failure_total,
            "retry_total": self.retry_total,
            "retry_exhausted_total": self.retry_exhausted_total,
            "timeout_total": self.timeout_total,
            "worker_restart_total": self.worker_restart_total,
            "watchdog_trigger_total": self.watchdog_trigger_total,
            "graceful_shutdown_total": self.graceful_shutdown_total,
            "unclean_shutdown_total": self.unclean_shutdown_total,
            "event_expired_total": self.event_expired_total,
        }
