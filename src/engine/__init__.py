"""Event Engine — exports públicos.

Esta é a interface pública do engine. Importar daqui, não dos submódulos.
"""

from __future__ import annotations

from src.engine.aggregator import EventAggregator
from src.engine.config import EngineConfig
from src.engine.dedup import DeduplicationCache
from src.engine.dispatcher import (
    Dispatcher,
    EventConsumer,
    LoggingConsumer,
    NullConsumer,
)
from src.engine.errors import (
    AggregationError,
    EngineError,
    EngineShutdownError,
    HandlerError,
    QueueFullError,
)
from src.engine.metrics import EngineMetrics, LatencyTracker
from src.engine.processor import EventProcessor
from src.engine.queue import PriorityQueueSet

__all__ = [
    # Config
    "EngineConfig",
    # Core
    "EventProcessor",
    "PriorityQueueSet",
    "DeduplicationCache",
    "EventAggregator",
    "Dispatcher",
    # Consumers
    "EventConsumer",
    "NullConsumer",
    "LoggingConsumer",
    # Metrics
    "EngineMetrics",
    "LatencyTracker",
    # Errors
    "EngineError",
    "QueueFullError",
    "HandlerError",
    "EngineShutdownError",
    "AggregationError",
]
