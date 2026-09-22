"""Event Processor — orquestrador do pipeline do Event Engine.

Pipeline de processamento:
1. receive(event) → validação básica
2. identify_priority(event) → resolução de prioridade (type + payload)
3. deduplicate(event) → verificação no cache de dedup
4. aggregate_or_enqueue(event) → aggregator ou queue
5. workers consomem da queue → handle(event) → dispatch

Concorrência:
- N workers async (pool fixo, configurável)
- asyncio.Semaphore(max_in_flight) limita eventos em processamento simultâneo
- Workers são criados em start() e cancelados em stop()
- Nenhum create_task livre — todo create_task tem referência e controle

Shutdown:
- stop() sinaliza shutdown, aguarda drain de P0 (timeout configurável)
- Workers encerram após o shutdown signal sem processar mais eventos normais
- Eventos P0 ainda são processados durante o drain period
- Tasks são canceladas e aguardadas — sem tasks órfãs

Falha de handlers:
- Exception de um handler → evento marcado FAILED, log, métrica
- O worker continua para o próximo evento
- Sem retry automático nesta fase
- Interface para retry/dead-letter futuro: _on_failure(event, exc)

Aggregation flush:
- Um loop separado chama aggregator.flush_expired() periodicamente
- Resultados são re-enfileirados como eventos normais (Event) ou
  despachados diretamente como AggregatedEvent
- O loop encerra durante shutdown
"""

from __future__ import annotations

import asyncio
from asyncio import Task
from datetime import datetime, timezone
from typing import Any

from src.domain.events import AggregatedEvent, Event, EventStatus
from src.domain.priorities import DEFAULT_PRIORITY_BY_EVENT_TYPE, Priority
from src.engine.aggregator import EventAggregator
from src.engine.config import EngineConfig
from src.engine.dedup import DeduplicationCache
from src.engine.dispatcher import Dispatcher
from src.engine.errors import EngineShutdownError, HandlerError, QueueFullError
from src.engine.metrics import EngineMetrics, now_ms
from src.engine.queue import PriorityQueueSet
from src.logging import get_logger
from src.observability.health import Watchdog, ComponentHealth
from src.observability.audit import EventAuditLogger
from src.observability.resilience import ResilienceMetrics

LOGGER = get_logger(__name__)

# Intervalo de flush do aggregator em segundos
_AGGREGATOR_FLUSH_INTERVAL = 0.25


class EventProcessor:
    """Orquestrador do pipeline do Event Engine."""

    def __init__(
        self,
        config: EngineConfig,
        metrics: EngineMetrics,
        dispatcher: Dispatcher,
        watchdog: Watchdog | None = None,
        audit_logger: EventAuditLogger | None = None,
        resilience: ResilienceMetrics | None = None,
    ) -> None:
        self._config = config
        self._metrics = metrics
        self._dispatcher = dispatcher
        self._audit_logger = audit_logger
        self._resilience = resilience

        self._queue = PriorityQueueSet(config, metrics)
        self._dedup = DeduplicationCache(
            maxsize=config.dedup_maxsize,
            ttl_seconds=config.dedup_ttl_seconds,
        )
        self._aggregator = EventAggregator(config, metrics)
        self._semaphore = asyncio.Semaphore(config.max_in_flight)

        self._shutdown = asyncio.Event()
        self._worker_tasks: list[Task[None]] = []
        self._flush_task: Task[None] | None = None
        self._dedup_evict_task: Task[None] | None = None
        self._started = False

        # Registra no watchdog se fornecido
        self._health: ComponentHealth | None = None
        if watchdog:
            self._health = watchdog.register("EventProcessor")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Inicia os workers e os loops de manutenção.

        Idempotente: chamar mais de uma vez não cria workers duplicados.
        """
        if self._started:
            return
        self._started = True
        self._shutdown.clear()

        n = self._config.n_workers
        for i in range(n):
            task = asyncio.create_task(
                self._worker_loop(worker_id=i),
                name=f"engine-worker-{i}",
            )
            self._worker_tasks.append(task)

        self._flush_task = asyncio.create_task(
            self._aggregator_flush_loop(),
            name="engine-aggregator-flush",
        )
        self._dedup_evict_task = asyncio.create_task(
            self._dedup_evict_loop(),
            name="engine-dedup-evict",
        )

        LOGGER.info(
            "EventProcessor iniciado com %d workers, max_in_flight=%d",
            n,
            self._config.max_in_flight,
        )

    async def stop(self) -> None:
        """Shutdown coordenado.

        1. Sinaliza shutdown (novos receives são rejeitados)
        2. Aguarda drain dos eventos P0 por até shutdown_drain_timeout_seconds
        3. Fecha a fila (workers saem do loop)
        4. Cancela tasks de manutenção
        5. Aguarda todos os workers encerrarem
        """
        if not self._started:
            return

        LOGGER.info("EventProcessor: iniciando shutdown")
        self._shutdown.set()

        # Drain de P0 com timeout
        drain_timeout = self._config.shutdown_drain_timeout_seconds
        try:
            await asyncio.wait_for(
                self._drain_p0(),
                timeout=drain_timeout,
            )
        except asyncio.TimeoutError:
            LOGGER.warning(
                "EventProcessor: timeout no drain P0 após %.1fs", drain_timeout
            )
            if self._resilience:
                self._resilience.record_timeout()

        # Fechar a fila (workers saem do get_next())
        self._queue.close()

        # Cancelar tasks de manutenção
        for task in [self._flush_task, self._dedup_evict_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        # Aguardar workers
        if self._worker_tasks:
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)
            self._worker_tasks.clear()

        # Flush final do aggregator (abandonar buckets pendentes)
        self._aggregator.clear()

        # Registra shutdown graceful (drain completou dentro do timeout)
        if self._resilience:
            self._resilience.record_graceful_shutdown()

        LOGGER.info(
            "EventProcessor encerrado. Métricas finais: %s",
            {
                "processed": self._metrics.events_processed,
                "dropped": self._metrics.events_dropped,
                "failed": self._metrics.events_failed,
            },
        )

    @property
    def is_running(self) -> bool:
        return self._started and not self._shutdown.is_set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def receive(self, event: Event) -> None:
        """Recebe um evento e o insere no pipeline.

        Esta é a única porta de entrada do Event Engine. O caller
        (ex: o loop que consome o TikTokLiveConnector) chama este método.

        O método é assíncrono mas rápido — não bloqueia esperando o
        evento ser processado. O processamento acontece nos workers.

        Raises:
            EngineShutdownError: quando o engine está encerrando
        """
        if self._shutdown.is_set():
            raise EngineShutdownError("EventProcessor está encerrando")

        self._metrics.record_received()

        # 1. Identificar prioridade
        priority = self._resolve_priority(event)

        # 2. Deduplicação
        if self._dedup.is_duplicate(event):
            self._metrics.record_deduplicated()
            LOGGER.debug(
                "Evento deduplicated: type=%s id=%s",
                event.event_type.value,
                event.event_id,
            )
            return

        # 3. Tentar agregar ou enfileirar
        ingest_time = now_ms()
        result = self._aggregator.try_aggregate(event, priority)

        if result is None:
            # Evento absorvido pelo aggregator — será emitido no flush
            return

        if isinstance(result, AggregatedEvent):
            # Bucket fechou com este evento: despachar aggregate imediatamente
            await self._dispatch_direct(result)
            return

        # result é o evento original (não elegível para agregação)
        # Enfileirar com timestamp de ingestão para medir queue_wait
        await self._enqueue(result, priority, ingest_time)

    def metrics_snapshot(self) -> dict[str, Any]:
        """Retorna snapshot das métricas atuais."""
        snap = self._metrics.snapshot()
        snap["dedup_cache"] = self._dedup.snapshot()
        snap["aggregator_pending_buckets"] = self._aggregator.pending_buckets()
        snap["dispatcher"] = self._dispatcher.snapshot()
        return snap

    # ------------------------------------------------------------------
    # Internal pipeline
    # ------------------------------------------------------------------

    def _resolve_priority(self, event: Event) -> Priority:
        """Resolve a prioridade de um evento.

        Baseline: DEFAULT_PRIORITY_BY_EVENT_TYPE (ver priorities.py).
        Extensão futura: Gift Mapping Engine pode sobrescrever P1 para
        gifts específicos de alto valor (ver DECISIONS.md).
        """
        return DEFAULT_PRIORITY_BY_EVENT_TYPE.get(event.event_type, Priority.P4)

    async def _enqueue(
        self, event: Event, priority: Priority, ingest_time: float
    ) -> None:
        """Tenta enfileirar o evento com tratamento de overflow."""
        try:
            self._queue.put_nowait(event, priority)
            self._metrics.record_accepted()
        except QueueFullError as exc:
            should_log = self._metrics.record_dropped(
                log_every_n=self._config.log_every_n_drops
            )
            if should_log:
                LOGGER.warning(
                    "Evento descartado por overflow (prioridade P%s): "
                    "depth=%d, capacity=%d. [Este log é emitido a cada %d drops]",
                    exc.priority,
                    exc.depth,
                    exc.capacity,
                    self._config.log_every_n_drops,
                )
        except EngineShutdownError:
            # Engine encerrando — descarte silencioso
            self._metrics.record_dropped()

    async def _dispatch_direct(self, event: AggregatedEvent) -> None:
        """Despacha um AggregatedEvent diretamente sem passar pela queue."""
        try:
            await self._dispatcher.dispatch(event)
        except Exception as exc:
            LOGGER.error(
                "Falha ao despachar AggregatedEvent %s: %s",
                event.aggregate_id,
                type(exc).__name__,
            )

    # ------------------------------------------------------------------
    # Worker loop
    # ------------------------------------------------------------------

    async def _worker_loop(self, worker_id: int) -> None:
        """Loop principal de worker."""
        self._metrics.record_worker_started()
        LOGGER.debug("Worker %d iniciado", worker_id)

        try:
            while True:
                # Ping watchdog to show we are not stuck
                if self._health:
                    self._health.mark_active()

                try:
                    event = await self._queue.get_next()
                except StopAsyncIteration:
                    break

                queue_dequeue_time = now_ms()

                async with self._semaphore:
                    await self._process_event(event, queue_dequeue_time)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            LOGGER.exception(
                "Worker %d encerrou com erro inesperado: %s",
                worker_id,
                type(exc).__name__,
            )
            if self._health:
                self._health.record_failure(
                    f"Worker {worker_id} crashed: {exc}", is_critical=True
                )
            if self._resilience:
                self._resilience.record_unclean_shutdown()
                self._resilience.record_recovery(success=False)
        finally:
            self._metrics.record_worker_stopped()
            LOGGER.debug("Worker %d encerrado", worker_id)

    async def _process_event(self, event: Event, queue_dequeue_time: float) -> None:
        """Processa um único evento do começo ao fim."""
        self._metrics.record_processing_start()
        start_time = now_ms()

        try:
            await self._dispatcher.dispatch(event)
            self._metrics.record_processing_end(failed=False)
            latency = now_ms() - start_time
            self._metrics.latency_processing.record(latency)
            self._metrics.latency_queue_wait.record(start_time - queue_dequeue_time)
            self._metrics.latency_end_to_end.record(
                now_ms() - event.timestamp.timestamp() * 1000
            )

            if self._audit_logger:
                self._audit_logger.log_event(
                    {
                        "event_id": event.event_id,
                        "event_type": event.event_type.value,
                        "status": "processed",
                        "latency_ms": round(latency, 2),
                        "queue_wait_ms": round(start_time - queue_dequeue_time, 2),
                    }
                )

            if self._health:
                self._health.record_success()

        except Exception as exc:
            self._metrics.record_processing_end(failed=True)
            LOGGER.error(
                "Falha ao processar evento %s (type=%s): %s",
                event.event_id,
                event.event_type.value,
                type(exc).__name__,
            )

            if self._audit_logger:
                self._audit_logger.log_event(
                    {
                        "event_id": event.event_id,
                        "event_type": event.event_type.value,
                        "status": "failed",
                        "error": str(exc),
                    }
                )

            if self._health:
                self._health.record_failure(f"Process error: {exc}", is_critical=False)

            self._on_failure(event, exc)

    def _on_failure(self, event: Event, exc: Exception) -> None:
        """Hook para tratamento de falhas de processamento."""
        pass

    # ------------------------------------------------------------------
    # Maintenance loops
    # ------------------------------------------------------------------

    async def _aggregator_flush_loop(self) -> None:
        """Loop periódico que flush o aggregator e reenfileira ou despacha resultados."""
        while not self._shutdown.is_set():
            try:
                await asyncio.sleep(_AGGREGATOR_FLUSH_INTERVAL)
            except asyncio.CancelledError:
                break

            results = self._aggregator.flush_expired()
            for result in results:
                if isinstance(result, AggregatedEvent):
                    await self._dispatch_direct(result)
                elif isinstance(result, Event):
                    # Evento single que estava no bucket — reenfileirar
                    priority = self._resolve_priority(result)
                    await self._enqueue(result, priority, now_ms())

    async def _dedup_evict_loop(self) -> None:
        """Loop periódico que limpa entradas expiradas do cache de dedup."""
        # Intervalo: metade do TTL configurado para garantir que entradas
        # expiradas sejam removidas razoavelmente cedo sem overhead excessivo
        interval = max(1.0, self._config.dedup_ttl_seconds / 2)
        while not self._shutdown.is_set():
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

            removed = self._dedup.evict_expired()
            if removed > 0:
                LOGGER.debug("Dedup cache: %d entradas expiradas removidas", removed)

    async def _drain_p0(self) -> None:
        """Aguarda a fila P0 esvaziar (sem processar novos eventos)."""
        while self._queue.depth_by_priority().get(0, 0) > 0:
            await asyncio.sleep(0.05)
