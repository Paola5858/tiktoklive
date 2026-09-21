"""Estado temporário, bounded e com TTL da avaliação de regras."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from time import monotonic
from typing import Callable

from src.domain.events import Event
from src.interaction.models import AggregationSpec, InteractionRule


class TTLSet:
    def __init__(self, maxsize: int, ttl_seconds: float, now: Callable[[], float] | None = None) -> None:
        if maxsize <= 0 or ttl_seconds <= 0:
            raise ValueError("TTLSet exige limites positivos")
        self.maxsize = maxsize
        self.ttl_seconds = ttl_seconds
        self.now = now or monotonic
        self._values: dict[str, float] = {}
        self._order: deque[str] = deque()

    def _cleanup(self) -> None:
        now = self.now()
        while self._order and (self._values.get(self._order[0], 0) <= now or len(self._order) > self.maxsize):
            key = self._order.popleft()
            self._values.pop(key, None)

    def contains(self, key: str) -> bool:
        self._cleanup()
        expiry = self._values.get(key)
        return expiry is not None and expiry > self.now()

    def add(self, key: str) -> None:
        self._cleanup()
        if key in self._values:
            return
        self._values[key] = self.now() + self.ttl_seconds
        self._order.append(key)
        self._cleanup()

    @property
    def size(self) -> int:
        self._cleanup()
        return len(self._values)


class CooldownManager:
    def __init__(self, max_states: int = 10_000, now: Callable[[], float] | None = None) -> None:
        self._states = TTLSet(max_states, 86_400, now)
        self.hits = 0

    def _key(self, rule: InteractionRule, event: Event) -> str:
        scope = rule.cooldown.scope
        if scope == "global":
            return f"rule:{rule.rule_id}:global"
        if scope == "user":
            return f"rule:{rule.rule_id}:user:{event.user.external_id or event.user.display_name}"
        if scope == "gift":
            return f"rule:{rule.rule_id}:gift:{event.payload.get('gift_id', event.payload.get('gift_name', ''))}"
        return f"rule:{rule.rule_id}"

    def blocked(self, rule: InteractionRule, event: Event) -> bool:
        if rule.cooldown.seconds <= 0:
            return False
        key = self._key(rule, event)
        state = TTLSet(self._states.maxsize, rule.cooldown.seconds, self._states.now)
        # Reuse the shared dictionary while allowing each rule's TTL to differ.
        state._values = self._states._values
        state._order = self._states._order
        blocked = state.contains(key)
        if blocked:
            self.hits += 1
        return blocked

    def mark(self, rule: InteractionRule, event: Event) -> None:
        if rule.cooldown.seconds <= 0:
            return
        key = self._key(rule, event)
        self._states.ttl_seconds = rule.cooldown.seconds
        self._states.add(key)


@dataclass
class _Bucket:
    started: float
    count: int
    event: Event


class AggregationManager:
    def __init__(self, max_states: int = 5_000, now: Callable[[], float] | None = None) -> None:
        self.max_states = max_states
        self.now = now or monotonic
        self._buckets: dict[str, _Bucket] = {}
        self.created = 0

    def _key(self, rule: InteractionRule, event: Event) -> str:
        key = rule.aggregation.key
        if key == "gift_id":
            value = event.payload.get("gift_id", "")
        elif key == "keyword":
            value = event.payload.get("text", "").lower()
        elif key == "user":
            value = event.user.external_id or event.user.display_name
        else:
            value = event.event_type.value
        return f"{rule.rule_id}:{key}:{value}"

    def accept(self, rule: InteractionRule, event: Event) -> bool:
        spec: AggregationSpec = rule.aggregation
        if spec.threshold <= 0:
            return True
        now = self.now()
        key = self._key(rule, event)
        bucket = self._buckets.get(key)
        if bucket is None or now - bucket.started > spec.window_seconds:
            if len(self._buckets) >= self.max_states:
                oldest = min(self._buckets, key=lambda item: self._buckets[item].started)
                self._buckets.pop(oldest, None)
            bucket = _Bucket(now, 0, event)
            self._buckets[key] = bucket
        bucket.count += 1
        if bucket.count >= spec.threshold:
            self._buckets.pop(key, None)
            self.created += 1
            return True
        return False

    @property
    def size(self) -> int:
        now = self.now()
        expired = [key for key, bucket in self._buckets.items() if now - bucket.started > 300]
        for key in expired:
            self._buckets.pop(key, None)
        return len(self._buckets)


class ActionRateLimiter:
    def __init__(self, global_per_second: int = 100, per_user_per_second: int = 20, max_states: int = 10_000, now: Callable[[], float] | None = None) -> None:
        if global_per_second <= 0 or per_user_per_second <= 0:
            raise ValueError("rate limits devem ser positivos")
        self.global_limit = global_per_second
        self.user_limit = per_user_per_second
        self.now = now or monotonic
        self.global_hits: deque[float] = deque()
        self.user_hits: dict[str, deque[float]] = {}
        self.max_states = max_states
        self.hits = 0

    def allow(self, event: Event, priority: int) -> bool:
        now = self.now()
        while self.global_hits and self.global_hits[0] <= now - 1:
            self.global_hits.popleft()
        if len(self.global_hits) >= self.global_limit and priority > 0:
            self.hits += 1
            return False
        user_key = event.user.external_id or event.user.display_name
        queue = self.user_hits.setdefault(user_key, deque())
        while queue and queue[0] <= now - 1:
            queue.popleft()
        if len(queue) >= self.user_limit and priority > 1:
            self.hits += 1
            return False
        self.global_hits.append(now)
        queue.append(now)
        if len(self.user_hits) > self.max_states:
            self.user_hits.pop(next(iter(self.user_hits)))
        return True
