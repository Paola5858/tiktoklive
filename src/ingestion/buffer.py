"""Fronteira bounded entre callbacks da biblioteca e o consumidor interno."""

from __future__ import annotations

import asyncio
from collections import deque

from src.domain.events import Event


class BoundedEventBuffer:
    """Buffer FIFO limitado, sem bloquear o callback externo quando cheio."""

    def __init__(self, maxsize: int) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize precisa ser maior que zero")
        self._items: deque[Event] = deque()
        self._maxsize = maxsize
        self._ready = asyncio.Event()
        self._closed = False

    @property
    def maxsize(self) -> int:
        return self._maxsize

    def put_nowait(self, event: Event) -> bool:
        """Insere ou retorna False quando o limite foi atingido."""
        if self._closed or len(self._items) >= self._maxsize:
            return False
        self._items.append(event)
        self._ready.set()
        return True

    async def get(self) -> Event:
        while True:
            if self._items:
                event = self._items.popleft()
                if not self._items:
                    self._ready.clear()
                return event
            if self._closed:
                raise StopAsyncIteration
            await self._ready.wait()

    def close(self) -> None:
        self._closed = True
        self._ready.set()

    def __len__(self) -> int:
        return len(self._items)
