"""Connector isolado para TikTokLive 7.x.

A biblioteca externa fica confinada neste módulo. O restante do projeto recebe
somente `Event` através de `events()`.
"""

from __future__ import annotations

import asyncio
from asyncio import CancelledError, Task
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from typing import Any

from TikTokLive import TikTokLiveClient
from TikTokLive.events import (
    CommentEvent,
    ConnectEvent,
    DisconnectEvent,
    FollowEvent,
    GiftEvent,
    LiveEndEvent,
)

from src.domain.events import Event
from src.logging import get_logger
from src.ingestion.base import ConnectionState, ConnectorMetrics
from src.ingestion.buffer import BoundedEventBuffer
from src.ingestion.normalizer import (
    EventNormalizationError,
    normalize_comment,
    normalize_follow,
    normalize_gift,
)
from src.observability.health import Watchdog, ComponentHealth

LOGGER = get_logger(__name__)
ClientFactory = Callable[[str], Any]


class TikTokLiveConnector:
    """Conecta a uma LIVE, normaliza comment/gift/follow e expõe eventos."""

    def __init__(
        self,
        unique_id: str,
        *,
        max_buffer_size: int = 1000,
        max_reconnect_attempts: int = 5,
        backoff_delays: tuple[float, ...] = (2.0, 4.0, 8.0, 16.0, 30.0),
        client_factory: ClientFactory | None = None,
        watchdog: Watchdog | None = None,
    ) -> None:
        unique_id = unique_id.strip().lstrip("@")
        if not unique_id:
            raise ValueError("unique_id não pode ser vazio")
        if max_reconnect_attempts < 0:
            raise ValueError("max_reconnect_attempts não pode ser negativo")
        if not backoff_delays or any(delay < 0 for delay in backoff_delays):
            raise ValueError("backoff_delays precisa conter atrasos não negativos")

        self.unique_id = unique_id
        self.max_reconnect_attempts = max_reconnect_attempts
        self.backoff_delays = backoff_delays
        self.metrics = ConnectorMetrics()
        self._state = ConnectionState.DISCONNECTED
        self._stop_event = asyncio.Event()
        self._runner_task: Task[None] | None = None
        self._client_task: Task[Any] | None = None
        self._client: Any | None = None
        self._live_ended = False
        self._buffer = BoundedEventBuffer(max_buffer_size)
        self._client_factory = client_factory or self._default_client_factory

        self._health: ComponentHealth | None = None
        if watchdog:
            self._health = watchdog.register("TikTokConnector")

    @staticmethod
    def _default_client_factory(unique_id: str) -> TikTokLiveClient:
        return TikTokLiveClient(unique_id=unique_id)

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def buffer_depth(self) -> int:
        return len(self._buffer)

    def start(self) -> Task[None]:
        """Inicia uma task controlada; chamar `stop()` para encerrá-la."""
        if self._runner_task and not self._runner_task.done():
            return self._runner_task
        if self._state in {
            ConnectionState.FAILED,
            ConnectionState.STOPPING,
            ConnectionState.STOPPED,
        }:
            raise RuntimeError("connector não pode ser reiniciado após shutdown")
        self._stop_event.clear()
        self._live_ended = False
        self._runner_task = asyncio.create_task(self._run(), name="tiktok-live-connector")
        return self._runner_task

    async def wait(self) -> None:
        """Aguarda o lifecycle terminar, útil para o entrypoint da aplicação."""
        if self._runner_task:
            await self._runner_task

    async def stop(self) -> None:
        """Shutdown idempotente, inclusive durante conexão ou backoff."""
        runner_active = self._runner_task is not None and not self._runner_task.done()
        if self._state is ConnectionState.STOPPED or (
            self._state is ConnectionState.DISCONNECTED and not runner_active
        ):
            self._state = ConnectionState.STOPPED
            self._buffer.close()
            return

        self._state = ConnectionState.STOPPING
        self._stop_event.set()
        client = self._client
        if client is not None:
            try:
                client.disconnect(close_client=True)
            except Exception as exc:  # shutdown não deve esconder o erro original
                self.metrics.last_error = type(exc).__name__
                LOGGER.warning("TikTok disconnect falhou durante shutdown: %s", type(exc).__name__)

        current = asyncio.current_task()
        if self._runner_task and self._runner_task is not current:
            await self._runner_task
        self._state = ConnectionState.STOPPED
        self._buffer.close()

    async def events(self) -> AsyncIterator[Event]:
        """Entrega eventos normalizados até o connector ser encerrado."""
        while True:
            try:
                yield await self._buffer.get()
            except StopAsyncIteration:
                return

    async def _run(self) -> None:
        reconnect_attempts = 0
        while not self._stop_event.is_set():
            self.metrics.connection_attempts += 1
            self._state = (
                ConnectionState.CONNECTING
                if reconnect_attempts == 0
                else ConnectionState.RECONNECTING
            )
            if reconnect_attempts:
                self.metrics.reconnect_attempts += 1

            try:
                self._client = self._client_factory(self.unique_id)
                self._register_listeners(self._client)
                self._client_task = await self._client.start(fetch_live_check=True)
                await self._client_task
                if self._live_ended or self._stop_event.is_set():
                    break
                self.metrics.disconnects += 1
                raise ConnectionError("TikTok client terminou sem sinal de encerramento da LIVE")
            except CancelledError:
                raise
            except Exception as exc:
                self.metrics.last_error = type(exc).__name__
                self.metrics.disconnects += 1
                if self._stop_event.is_set() or self._live_ended:
                    break
                if reconnect_attempts >= self.max_reconnect_attempts:
                    self._state = ConnectionState.FAILED
                    LOGGER.error("TikTok connector falhou após tentativas limitadas: %s", type(exc).__name__)
                    return
                delay = self.backoff_delays[min(reconnect_attempts, len(self.backoff_delays) - 1)]
                reconnect_attempts += 1
                LOGGER.warning("TikTok desconectado; retry em %.1fs: %s", delay, type(exc).__name__)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
            finally:
                client = self._client
                self._client = None
                self._client_task = None
                if client is not None:
                    try:
                        client.disconnect(close_client=True)
                    except Exception as exc:
                        self.metrics.last_error = type(exc).__name__
                        LOGGER.warning("TikTok disconnect falhou: %s", type(exc).__name__)

        if self._state not in {ConnectionState.FAILED, ConnectionState.STOPPING}:
            self._state = ConnectionState.STOPPED
        self._buffer.close()

    def _register_listeners(self, client: Any) -> None:
        client.add_listener(ConnectEvent, self._on_connect)
        client.add_listener(DisconnectEvent, self._on_disconnect)
        client.add_listener(LiveEndEvent, self._on_live_end)
        client.add_listener(CommentEvent, self._on_comment)
        client.add_listener(GiftEvent, self._on_gift)
        client.add_listener(FollowEvent, self._on_follow)

    async def _on_connect(self, event: ConnectEvent) -> None:
        self._state = ConnectionState.CONNECTED
        self.metrics.successful_connections += 1
        self.metrics.last_successful_connection = datetime.now(timezone.utc)
        if self._health:
            self._health.record_success()
            self._health.state = ComponentState.READY

    async def _on_disconnect(self, _event: DisconnectEvent) -> None:
        self.metrics.disconnects += 1
        if self._health:
            self._health.record_failure("Disconnected from TikTok", is_critical=False)
            self._health.state = ComponentState.DISCONNECTED

    async def _on_live_end(self, _event: LiveEndEvent) -> None:
        self._live_ended = True
        self._stop_event.set()
        if self._client is not None:
            self._client.disconnect(close_client=True)
        if self._health:
            self._health.state = ComponentState.STOPPED
            self._health.record_success()

    async def _on_comment(self, raw_event: CommentEvent) -> None:
        if self._health:
            self._health.mark_active()
        await self._normalize_and_buffer(normalize_comment, raw_event)

    async def _on_gift(self, raw_event: GiftEvent) -> None:
        if self._health:
            self._health.mark_active()
        await self._normalize_and_buffer(normalize_gift, raw_event)

    async def _on_follow(self, raw_event: FollowEvent) -> None:
        if self._health:
            self._health.mark_active()
        await self._normalize_and_buffer(normalize_follow, raw_event)

    async def _normalize_and_buffer(self, normalizer: Callable[..., Event], raw_event: Any) -> None:
        received_at = datetime.now(timezone.utc)
        self.metrics.events_received += 1
        self.metrics.last_received_event = received_at
        try:
            event = normalizer(raw_event, received_at=received_at)
        except EventNormalizationError as exc:
            self.metrics.events_rejected += 1
            LOGGER.warning("Evento TikTok rejeitado: %s", str(exc))
            return
        except Exception as exc:
            self.metrics.events_rejected += 1
            self.metrics.last_error = type(exc).__name__
            LOGGER.exception("Falha inesperada ao normalizar evento TikTok: %s", type(exc).__name__)
            return

        self.metrics.record_normalized(event)
        if not self._buffer.put_nowait(event):
            self.metrics.events_dropped += 1
            LOGGER.warning("Buffer TikTok cheio; evento %s descartado", event.event_type.value)
