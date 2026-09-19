"""Dispatcher do Event Engine.

Responsabilidade: separar a decisão de processamento da entrega ao consumer.

O Dispatcher mantém um registry de consumers e despacha eventos para todos
que declararem `can_handle(event) == True`. Falhas de um consumer são
isoladas — outros consumers continuam recebendo o evento.

Design:
- EventConsumer: Protocol (duck typing) — consumer não precisa herdar de nada
- Dispatcher: registro de consumers com despacho tolerante a falhas
- Consumidores são substituíveis sem alterar a fila ou o processor
- Adicionar um novo consumer não exige reescrita

Consumers disponíveis nesta fase:
- NullConsumer: só loga, sem efeito externo (placeholder para testes)
- LoggingConsumer: log estruturado de todos os eventos processados

O Roblox bridge (consumer real) será adicionado na Fase 4.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from src.domain.events import AggregatedEvent, Event
from src.engine.errors import HandlerError
from src.logging import get_logger

if TYPE_CHECKING:
    from src.engine.metrics import EngineMetrics

LOGGER = get_logger(__name__)


@runtime_checkable
class EventConsumer(Protocol):
    """Protocolo que todo consumer do Event Engine deve implementar.

    Implementar este protocolo não requer herança — qualquer objeto com
    `can_handle` e `handle` satisfaz o tipo.

    O método `handle` pode ser sync ou async. O Dispatcher suporta ambos.
    """

    @property
    def name(self) -> str:
        """Nome único do consumer para logging e métricas."""
        ...

    def can_handle(self, event: Event | AggregatedEvent) -> bool:
        """Retorna True se este consumer quer processar o evento.

        Consumers podem filtrar por tipo, prioridade, source, etc.
        Retornar False significa pular silenciosamente (sem erro, sem log).
        """
        ...

    async def handle(self, event: Event | AggregatedEvent) -> None:
        """Processa o evento.

        Deve retornar normalmente em caso de sucesso.
        Lançar exception em caso de falha — o Dispatcher vai isolar e registrar.
        Não deve fazer retry interno — essa responsabilidade é do Dispatcher/Processor.
        """
        ...


class Dispatcher:
    """Registry de consumers com despacho tolerante a falhas.

    O Dispatcher despacha cada evento para todos os consumers que declararem
    `can_handle(event) == True`. Falhas de um consumer são capturadas,
    registradas e isoladas — os outros consumers continuam recebendo o evento.

    Usage:
        dispatcher = Dispatcher(metrics)
        dispatcher.register(roblox_consumer)
        dispatcher.register(analytics_consumer)
        await dispatcher.dispatch(event)
    """

    def __init__(self, metrics: "EngineMetrics") -> None:
        self._consumers: list[EventConsumer] = []
        self._metrics = metrics

    def register(self, consumer: EventConsumer) -> None:
        """Registra um consumer. Consumers são chamados na ordem de registro."""
        if consumer not in self._consumers:
            self._consumers.append(consumer)
            LOGGER.info("Consumer registrado: %s", consumer.name)

    def unregister(self, consumer: EventConsumer) -> None:
        """Remove um consumer do registry."""
        if consumer in self._consumers:
            self._consumers.remove(consumer)
            LOGGER.info("Consumer removido: %s", consumer.name)

    @property
    def consumer_count(self) -> int:
        return len(self._consumers)

    async def dispatch(self, event: Event | AggregatedEvent) -> None:
        """Despacha o evento para todos os consumers elegíveis.

        Falhas de cada consumer são capturadas individualmente.
        Um consumer falhando não impede os outros de receber o evento.
        Todos os erros são registrados em métricas e logs.
        """
        if not self._consumers:
            # Sem consumers registrados: evento processado mas não entregue
            # (Normal durante desenvolvimento e testes unitários)
            return

        for consumer in self._consumers:
            if not consumer.can_handle(event):
                continue

            try:
                await consumer.handle(event)
                self._metrics.record_dispatched()
            except Exception as exc:
                self._metrics.record_dispatch_failure()
                event_id = getattr(event, "event_id", getattr(event, "aggregate_id", "?"))
                LOGGER.error(
                    "Consumer '%s' falhou ao processar evento %s: %s",
                    consumer.name,
                    event_id,
                    type(exc).__name__,
                )
                # Não re-lança — outros consumers continuam

    def snapshot(self) -> dict[str, Any]:
        """Retorna estado do dispatcher para observabilidade."""
        return {
            "consumers": [c.name for c in self._consumers],
            "consumer_count": len(self._consumers),
        }


# ---------------------------------------------------------------------------
# Built-in consumers
# ---------------------------------------------------------------------------


class NullConsumer:
    """Consumer placeholder que aceita tudo mas não faz nada.

    Útil para testes unitários e para manter o pipeline funcional
    mesmo quando nenhum consumer real está configurado.
    """

    @property
    def name(self) -> str:
        return "null"

    def can_handle(self, event: Event | AggregatedEvent) -> bool:
        return True

    async def handle(self, event: Event | AggregatedEvent) -> None:
        pass  # intencional


class LoggingConsumer:
    """Consumer que produz log estruturado de todos os eventos.

    Produz DEBUG para eventos normais para evitar poluir INFO durante
    operação normal. Eventos P0/P1 são logados em INFO.
    """

    @property
    def name(self) -> str:
        return "logging"

    def can_handle(self, event: Event | AggregatedEvent) -> bool:
        return True

    async def handle(self, event: Event | AggregatedEvent) -> None:
        from src.domain.priorities import Priority

        if isinstance(event, AggregatedEvent):
            LOGGER.debug(
                "AggregatedEvent dispatched: type=%s count=%d source=%s",
                event.event_type.value,
                event.count,
                event.source,
            )
        else:
            priority = getattr(event, "priority", None)
            log_fn = (
                LOGGER.info
                if priority in (Priority.P0, Priority.P1)
                else LOGGER.debug
            )
            log_fn(
                "Event dispatched: type=%s id=%s source=%s",
                event.event_type.value,
                event.event_id,
                event.source,
            )
