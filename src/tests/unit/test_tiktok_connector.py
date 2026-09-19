import asyncio
from types import SimpleNamespace

import pytest

from src.domain.events import EventType
from src.ingestion.base import ConnectionState
from src.ingestion.buffer import BoundedEventBuffer
from src.ingestion.tiktok import TikTokLiveConnector


class FakeClient:
    def __init__(self, unique_id: str, *, fail_start: bool = False):
        self.unique_id = unique_id
        self.listeners = {}
        self.fail_start = fail_start
        self.disconnected = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def add_listener(self, event_type, callback):
        self.listeners[event_type] = callback

    async def start(self, **_kwargs):
        self.started.set()
        if self.fail_start:
            raise ConnectionError("fake connection failure")
        await self.listeners[next(iter(self.listeners))](SimpleNamespace(unique_id=self.unique_id, room_id=1))
        return asyncio.create_task(self.release.wait())

    def disconnect(self, close_client=False):
        self.disconnected = close_client
        self.release.set()


@pytest.mark.asyncio
async def test_buffer_is_bounded_and_closes():
    from src.domain.events import Event, EventType, EventUser

    buffer = BoundedEventBuffer(maxsize=1)
    event = Event(EventType.COMMENT, "test", EventUser("paola"))

    assert buffer.put_nowait(event) is True
    assert buffer.put_nowait(event) is False
    assert await buffer.get() is event
    buffer.close()
    with pytest.raises(StopAsyncIteration):
        await buffer.get()


@pytest.mark.asyncio
async def test_connector_registers_listeners_and_shutdown_is_idempotent():
    clients = []

    def factory(unique_id):
        client = FakeClient(unique_id)
        clients.append(client)
        return client

    connector = TikTokLiveConnector(
        "@paola",
        client_factory=factory,
        max_reconnect_attempts=0,
        backoff_delays=(0,),
    )
    task = connector.start()
    await asyncio.sleep(0)
    await asyncio.wait_for(clients[0].started.wait(), timeout=1)
    assert connector.state is ConnectionState.CONNECTED
    assert connector.metrics.successful_connections == 1
    assert len(clients[0].listeners) == 6

    await connector.stop()
    await connector.stop()
    await asyncio.wait_for(task, timeout=1)
    assert connector.state is ConnectionState.STOPPED
    assert clients[0].disconnected is True


@pytest.mark.asyncio
async def test_connector_can_stop_immediately_after_start():
    clients = []

    def factory(unique_id):
        client = FakeClient(unique_id)
        clients.append(client)
        return client

    connector = TikTokLiveConnector("paola", client_factory=factory)
    task = connector.start()
    await connector.stop()
    await asyncio.wait_for(task, timeout=1)

    assert clients == []
    assert connector.state is ConnectionState.STOPPED


@pytest.mark.asyncio
async def test_connector_limits_reconnect_attempts_and_enters_failed():
    attempts = 0

    def factory(unique_id):
        nonlocal attempts
        attempts += 1
        return FakeClient(unique_id, fail_start=True)

    connector = TikTokLiveConnector(
        "paola",
        client_factory=factory,
        max_reconnect_attempts=2,
        backoff_delays=(0,),
    )
    task = connector.start()
    await asyncio.wait_for(task, timeout=1)

    assert attempts == 3
    assert connector.state is ConnectionState.FAILED
    assert connector.metrics.reconnect_attempts == 2


@pytest.mark.asyncio
async def test_connector_exposes_normalized_events_from_callbacks():
    client_ref = None

    def factory(unique_id):
        nonlocal client_ref
        client_ref = FakeClient(unique_id)
        return client_ref

    connector = TikTokLiveConnector("paola", client_factory=factory)
    task = connector.start()
    await asyncio.sleep(0)
    await asyncio.wait_for(client_ref.started.wait(), timeout=1)

    raw = SimpleNamespace(
        user=SimpleNamespace(id=7, nickname="viewer", unique_id="viewer"),
        common=SimpleNamespace(msg_id=8, create_time=1_758_298_800_000),
        content="oi",
    )
    await client_ref.listeners[next(k for k in client_ref.listeners if k.__name__ == "CommentEvent")](raw)

    event = await asyncio.wait_for(connector._buffer.get(), timeout=1)
    assert event.event_type is EventType.COMMENT
    assert event.payload["text"] == "oi"
    assert connector.metrics.events_normalized == 1

    await connector.stop()
    await asyncio.wait_for(task, timeout=1)
