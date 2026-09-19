"""Métricas do Event Engine.

Todos os contadores e rastreadores de latência do engine vivem aqui.
Nenhuma métrica é espalhada pelos módulos internos — eles recebem
`EngineMetrics` e chamam os métodos de registro.

Design:
- Contadores são simples ints mutáveis (não tem race em asyncio single-thread)
- LatencyTracker usa deque limitado para p50/p95 sem crescimento infinito
- Snapshot produz cópia imutável para exportação (API, logs, health check)
- Nenhuma métrica guarda payload ou dados de usuário

Invariante de memória:
- LatencyTracker.samples: deque(maxlen=N) — O(N) fixo
- depth_by_priority: dict fixo de 5 entradas — O(1)
- Todos os outros campos: int/float — O(1)
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


class LatencyTracker:
    """Rastreia amostras de latência com tamanho de buffer limitado.

    Mantém as N amostras mais recentes em um deque circular para calcular
    média, p50 e p95 sem crescer indefinidamente.

    Não usar para amostras pequenas (<20): p95 em amostras pequenas é
    enganoso (ver latency_budget na spec). O snapshot reporta o número
    de amostras disponíveis para que o consumidor decida se confia no percentil.
    """

    def __init__(self, maxlen: int = 1_000) -> None:
        if maxlen <= 0:
            raise ValueError("maxlen deve ser > 0")
        self._samples: deque[float] = deque(maxlen=maxlen)
        self._maxlen = maxlen

    def record(self, latency_ms: float) -> None:
        """Registra uma amostra de latência em milissegundos."""
        self._samples.append(latency_ms)

    @property
    def count(self) -> int:
        return len(self._samples)

    @property
    def maxlen(self) -> int:
        return self._maxlen

    def snapshot(self) -> dict[str, Any]:
        """Retorna estatísticas calculadas sobre as amostras disponíveis.

        Percentis são calculados apenas quando há amostras suficientes
        (>= 20). Abaixo disso, retorna None para evitar valores enganosos.
        """
        if not self._samples:
            return {"count": 0, "avg_ms": None, "p50_ms": None, "p95_ms": None}

        sorted_samples = sorted(self._samples)
        n = len(sorted_samples)
        avg = sum(sorted_samples) / n

        if n >= 20:
            p50 = sorted_samples[int(n * 0.50)]
            p95 = sorted_samples[int(n * 0.95)]
        else:
            p50 = None
            p95 = None

        return {
            "count": n,
            "avg_ms": round(avg, 3),
            "p50_ms": round(p50, 3) if p50 is not None else None,
            "p95_ms": round(p95, 3) if p95 is not None else None,
        }


@dataclass(slots=True)
class EngineMetrics:
    """Contadores e rastreadores de estado do Event Engine.

    Campos divididos em grupos:
    - Recepção: eventos que chegaram no engine
    - Deduplicação: eventos identificados como duplicatas
    - Agregação: eventos agrupados
    - Enfileiramento: aceitos vs descartados
    - Processamento: executados pelos workers
    - Despacho: entregues aos consumers
    - Workers: estado dos workers
    - Latência: rastreadores por fase
    """

    # Recepção
    events_received: int = 0
    events_accepted: int = 0
    events_rejected: int = 0  # falharam na validação

    # Deduplicação
    events_deduplicated: int = 0

    # Agregação
    events_aggregated: int = 0
    aggregated_batches_flushed: int = 0

    # Enfileiramento
    events_queued: int = 0
    events_dropped: int = 0
    events_overflow: int = 0
    _drops_since_last_log: int = 0

    # Processamento
    events_processing: int = 0
    events_processed: int = 0
    events_failed: int = 0

    # Despacho
    events_dispatched: int = 0
    dispatch_failures: int = 0

    # Workers
    active_workers: int = 0
    in_flight_events: int = 0

    # Latência — rastreadores por fase
    latency_ingest_to_queue: LatencyTracker = field(default_factory=LatencyTracker)
    latency_queue_wait: LatencyTracker = field(default_factory=LatencyTracker)
    latency_processing: LatencyTracker = field(default_factory=LatencyTracker)
    latency_end_to_end: LatencyTracker = field(default_factory=LatencyTracker)

    # Profundidade da fila por prioridade (atualizado pelo PriorityQueueSet)
    depth_by_priority: dict[int, int] = field(
        default_factory=lambda: {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}
    )

    def record_received(self) -> None:
        self.events_received += 1

    def record_accepted(self) -> None:
        self.events_accepted += 1

    def record_rejected(self) -> None:
        self.events_rejected += 1

    def record_deduplicated(self) -> None:
        self.events_deduplicated += 1

    def record_aggregated(self, count: int = 1) -> None:
        self.events_aggregated += count

    def record_aggregated_flush(self) -> None:
        self.aggregated_batches_flushed += 1

    def record_queued(self, priority: int) -> None:
        self.events_queued += 1
        self.depth_by_priority[priority] = self.depth_by_priority.get(priority, 0) + 1

    def record_dropped(self, log_every_n: int = 50) -> bool:
        """Registra drop. Retorna True quando deve emitir log (a cada N drops)."""
        self.events_dropped += 1
        self.events_overflow += 1
        self._drops_since_last_log += 1
        if self._drops_since_last_log >= log_every_n:
            self._drops_since_last_log = 0
            return True
        return False

    def record_processing_start(self) -> None:
        self.events_processing += 1
        self.in_flight_events += 1

    def record_processing_end(self, *, failed: bool = False) -> None:
        self.events_processing = max(0, self.events_processing - 1)
        self.in_flight_events = max(0, self.in_flight_events - 1)
        if failed:
            self.events_failed += 1
        else:
            self.events_processed += 1

    def record_dispatched(self) -> None:
        self.events_dispatched += 1

    def record_dispatch_failure(self) -> None:
        self.dispatch_failures += 1

    def record_worker_started(self) -> None:
        self.active_workers += 1

    def record_worker_stopped(self) -> None:
        self.active_workers = max(0, self.active_workers - 1)

    def update_queue_depth(self, priority: int, depth: int) -> None:
        self.depth_by_priority[priority] = depth

    def snapshot(self) -> dict[str, Any]:
        """Retorna snapshot imutável do estado atual das métricas."""
        return {
            "received": self.events_received,
            "accepted": self.events_accepted,
            "rejected": self.events_rejected,
            "deduplicated": self.events_deduplicated,
            "aggregated": self.events_aggregated,
            "aggregated_batches_flushed": self.aggregated_batches_flushed,
            "queued": self.events_queued,
            "dropped": self.events_dropped,
            "overflow": self.events_overflow,
            "processed": self.events_processed,
            "failed": self.events_failed,
            "dispatched": self.events_dispatched,
            "dispatch_failures": self.dispatch_failures,
            "active_workers": self.active_workers,
            "in_flight": self.in_flight_events,
            "queue_depth_by_priority": dict(self.depth_by_priority),
            "queue_depth_total": sum(self.depth_by_priority.values()),
            "latency": {
                "ingest_to_queue": self.latency_ingest_to_queue.snapshot(),
                "queue_wait": self.latency_queue_wait.snapshot(),
                "processing": self.latency_processing.snapshot(),
                "end_to_end": self.latency_end_to_end.snapshot(),
            },
        }


def now_ms() -> float:
    """Retorna o tempo atual em milissegundos (monotônico para latência)."""
    return time.monotonic() * 1000.0
