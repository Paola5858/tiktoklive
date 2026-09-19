"""Priority Queue do Event Engine.

Implementação: múltiplas filas por prioridade (P0→P4) com scheduler explícito.

Decisão arquitetural (ver DECISIONS.md):
- 5 asyncio.Queue separadas, uma por nível de prioridade
- Scheduler WRR (Weighted Round Robin) ativado quando há eventos em
  múltiplos níveis simultaneamente
- FIFO natural dentro de cada prioridade
- Profundidade observável por nível sem overhead de hash
- P0 (SYSTEM) tem "express lane" adicional para eventos críticos
  que não competem com os limites normais

Overflow policy (ver EngineConfig):
- P0: aceito até p0_express_capacity (express) + queue_capacity_per_level (normal)
- P1: drop quando fila total > overflow_threshold_p1 * total_capacity
- P2: drop quando > overflow_threshold_p2 * total_capacity
- P3: drop quando > overflow_threshold_p3 * total_capacity
- P4: drop quando > overflow_threshold_p4 * total_capacity

Invariante de memória:
- Cada asyncio.Queue tem maxsize configurado — não cresce além do limite
- Express lane P0 tem capacidade separada e configurável
- Nenhuma lista global sem limite

Fairness:
- WRR com pesos configuráveis (padrão: P0=100, P1=8, P2=4, P3=2, P4=1)
- Em condição de fila única (só uma prioridade tem eventos), drena
  diretamente sem overhead de round-robin
- P3/P4 recebem throughput proporcional ao peso durante carga mista,
  prevenindo starvation indefinido
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from src.domain.events import Event, EventType
from src.domain.priorities import Priority
from src.engine.errors import EngineShutdownError, QueueFullError

if TYPE_CHECKING:
    from src.engine.config import EngineConfig
    from src.engine.metrics import EngineMetrics

# Prioridades que SYSTEM events (express lane) são aceitos
_SYSTEM_EVENT_TYPES = frozenset({EventType.SYSTEM})


class PriorityQueueSet:
    """Conjunto de 5 filas bounded, uma por prioridade, com scheduler WRR.

    Usage:
        queue = PriorityQueueSet(config, metrics)
        await queue.put(event, Priority.P1)
        event = await queue.get_next()
        queue.close()
    """

    def __init__(self, config: "EngineConfig", metrics: "EngineMetrics") -> None:
        self._config = config
        self._metrics = metrics
        self._closed = False

        # 5 filas bounded, uma por prioridade
        cap = config.queue_capacity_per_level
        self._queues: dict[int, asyncio.Queue[Event]] = {
            Priority.P0: asyncio.Queue(maxsize=cap),
            Priority.P1: asyncio.Queue(maxsize=cap),
            Priority.P2: asyncio.Queue(maxsize=cap),
            Priority.P3: asyncio.Queue(maxsize=cap),
            Priority.P4: asyncio.Queue(maxsize=cap),
        }

        # Express lane P0 para eventos SYSTEM críticos (live_ended, shutdown)
        # Separada para não competir com outros P0 pelo mesmo limite
        self._p0_express: asyncio.Queue[Event] = asyncio.Queue(
            maxsize=config.p0_express_capacity
        )

        # Evento que acorda o scheduler quando qualquer fila tem item
        self._has_items = asyncio.Event()

        # Contadores WRR: quantos eventos foram consumidos do nível atual
        self._wrr_counts: dict[int, int] = {p: 0 for p in range(5)}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def put_nowait(self, event: Event, priority: Priority) -> None:
        """Enfileira um evento com a prioridade dada.

        Política de overflow:
        - SYSTEM events P0: tentam express lane primeiro, depois P0 normal
        - Demais eventos: verificam threshold de capacidade total por prioridade
        - Se overflow, lança QueueFullError (chamador decide se loga/dropa)

        Raises:
            QueueFullError: quando a política de overflow determina descarte
            EngineShutdownError: quando a fila está fechada
        """
        if self._closed:
            raise EngineShutdownError("PriorityQueueSet está fechado")

        p = int(priority)

        # Express lane para eventos SYSTEM críticos (P0 apenas)
        if p == Priority.P0 and event.event_type in _SYSTEM_EVENT_TYPES:
            if not self._p0_express.full():
                self._p0_express.put_nowait(event)
                self._has_items.set()
                self._metrics.record_queued(p)
                return
            # Express lane cheia: cai pro P0 normal

        # Verificar threshold de overflow por prioridade
        self._check_overflow(p, event)

        # Tentar inserir na fila normal
        q = self._queues[p]
        if q.full():
            raise QueueFullError(str(p), q.qsize(), q.maxsize)

        q.put_nowait(event)
        self._has_items.set()
        self._metrics.record_queued(p)
        self._metrics.update_queue_depth(p, q.qsize())

    async def get_next(self) -> Event:
        """Retorna o próximo evento respeitando prioridade e fairness WRR.

        Bloqueia até haver um evento disponível ou a fila ser fechada.

        Raises:
            StopAsyncIteration: quando a fila está fechada e vazia
        """
        while True:
            event = self._try_get_next()
            if event is not None:
                return event

            if self._closed and self._total_depth() == 0:
                raise StopAsyncIteration

            # Aguarda sinal de que há item em alguma fila
            self._has_items.clear()
            # Checar novamente após clear para evitar race condition
            event = self._try_get_next()
            if event is not None:
                return event
            if self._closed and self._total_depth() == 0:
                raise StopAsyncIteration
            await self._has_items.wait()

    def close(self) -> None:
        """Fecha a fila. Novos puts lançam EngineShutdownError.

        Itens já enfileirados continuam disponíveis para consumo
        até a fila esvaziar (shutdown graceful).
        """
        self._closed = True
        self._has_items.set()  # Acorda consumidores bloqueados

    def depth_by_priority(self) -> dict[int, int]:
        """Retorna profundidade atual de cada fila de prioridade."""
        return {
            p: self._queues[p].qsize() + (
                self._p0_express.qsize() if p == Priority.P0 else 0
            )
            for p in range(5)
        }

    def total_depth(self) -> int:
        """Profundidade total de todas as filas."""
        return self._total_depth()

    @property
    def is_closed(self) -> bool:
        return self._closed

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _total_depth(self) -> int:
        return sum(q.qsize() for q in self._queues.values()) + self._p0_express.qsize()

    def _check_overflow(self, priority: int, event: Event) -> None:
        """Lança QueueFullError se a política de overflow determinar descarte.

        P0 não tem threshold — é sempre tentado (exceto se a fila P0 em si estiver cheia).
        P1, P2, P3, P4 têm thresholds de fração da capacidade total.
        """
        if priority == Priority.P0:
            return  # P0 normal: checar apenas qsize na fila, feito em put_nowait

        total_capacity = self._config.total_queue_capacity
        current_total = self._total_depth()

        thresholds = {
            Priority.P1: self._config.overflow_threshold_p1,
            Priority.P2: self._config.overflow_threshold_p2,
            Priority.P3: self._config.overflow_threshold_p3,
            Priority.P4: self._config.overflow_threshold_p4,
        }

        threshold = thresholds.get(priority)
        if threshold is None:
            return

        if current_total >= total_capacity * threshold:
            q = self._queues[priority]
            raise QueueFullError(str(priority), current_total, total_capacity)

    def _try_get_next(self) -> Event | None:
        """Tenta obter o próximo evento sem bloquear.

        Algoritmo:
        1. Express lane P0 tem prioridade absoluta
        2. Para as filas normais, usa WRR: tenta a maior prioridade
           não-vazia até consumir seu peso, depois passa para a próxima
        3. Se apenas um nível tem eventos, drena diretamente (sem overhead WRR)
        """
        # Express lane sempre primeiro
        if not self._p0_express.empty():
            event = self._p0_express.get_nowait()
            self._metrics.update_queue_depth(Priority.P0, self._queues[Priority.P0].qsize() + self._p0_express.qsize())
            return event

        # Verificar quais prioridades têm eventos
        non_empty = [p for p in range(5) if not self._queues[p].empty()]
        if not non_empty:
            return None

        # Se só uma prioridade tem eventos, drena diretamente
        if len(non_empty) == 1:
            p = non_empty[0]
            self._wrr_counts[p] = 0
            event = self._queues[p].get_nowait()
            self._metrics.update_queue_depth(p, self._queues[p].qsize())
            return event

        # WRR: encontra o nível de maior prioridade disponível
        # dentro do orçamento de fairness
        weights = self._config.fairness_weights
        for p in range(5):  # P0 primeiro
            if self._queues[p].empty():
                self._wrr_counts[p] = 0
                continue
            weight = weights.get(p, 1)
            if self._wrr_counts[p] < weight:
                self._wrr_counts[p] += 1
                event = self._queues[p].get_nowait()
                self._metrics.update_queue_depth(p, self._queues[p].qsize())
                return event

        # Todos os pesos esgotados neste ciclo — resetar e recomeçar
        for p in range(5):
            self._wrr_counts[p] = 0
        # Tentar novamente após reset (recursão simples — só 1 nível)
        return self._try_get_next()
