"""Erros do Event Engine.

Separados dos erros de domínio (`src/domain/errors.py`) porque representam
falhas de infraestrutura do engine (fila cheia, handler falhando) e não
invariantes do modelo de domínio.
"""

from __future__ import annotations

from src.errors import AppError


class EngineError(AppError):
    """Erro base do Event Engine."""


class QueueFullError(EngineError):
    """A fila bounded atingiu o limite e a política de overflow determinou drop."""

    def __init__(self, priority: str, depth: int, capacity: int) -> None:
        super().__init__(
            f"Fila P{priority} cheia (depth={depth}, capacity={capacity}): evento descartado"
        )
        self.priority = priority
        self.depth = depth
        self.capacity = capacity


class HandlerError(EngineError):
    """Um handler ou consumer falhou ao processar um evento.

    A falha é isolada — outros eventos e outros consumers continuam operando.
    """

    def __init__(self, handler_name: str, event_id: str, cause: Exception) -> None:
        super().__init__(
            f"Handler '{handler_name}' falhou ao processar evento {event_id}: {cause}"
        )
        self.handler_name = handler_name
        self.event_id = event_id
        self.cause = cause


class EngineShutdownError(EngineError):
    """Operação rejeitada porque o engine está encerrando ou já encerrou."""


class AggregationError(EngineError):
    """Erro interno durante agregação de eventos."""
