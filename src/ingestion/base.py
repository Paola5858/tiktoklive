"""Contratos independentes da biblioteca externa de TikTok."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol

from src.domain.events import Event


class ConnectionState(str, Enum):
    """Estados observáveis do connector."""

    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class EventSink(Protocol):
    async def __call__(self, event: Event) -> None:
        """Recebe um evento já normalizado."""


@dataclass(slots=True)
class ConnectorMetrics:
    """Contadores e último estado do connector, sem guardar histórico em RAM."""

    connection_attempts: int = 0
    successful_connections: int = 0
    disconnects: int = 0
    reconnect_attempts: int = 0
    events_received: int = 0
    events_normalized: int = 0
    events_rejected: int = 0
    events_dropped: int = 0
    events_by_type: Counter[str] = field(default_factory=Counter)
    last_successful_connection: datetime | None = None
    last_received_event: datetime | None = None
    last_error: str | None = None

    def record_normalized(self, event: Event) -> None:
        self.events_normalized += 1
        self.events_by_type[event.event_type.value] += 1
