"""Motor declarativo entre Event normalizado e GameEvent de gameplay."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterable

from src.domain.events import Event
from src.interaction.models import ActionResult, ActionType, GameEvent, InteractionRule
from src.interaction.state import ActionRateLimiter, AggregationManager, CooldownManager, TTLSet


class RuleMetrics:
    def __init__(self) -> None:
        self.rules_evaluated = 0
        self.rules_matched = 0
        self.rules_rejected = 0
        self.actions_created = 0
        self.actions_dropped = 0
        self.cooldown_hits = 0
        self.rate_limit_hits = 0
        self.dedupe_hits = 0
        self.aggregations_created = 0
        self.expired_actions = 0
        self.handler_failures = 0

    def snapshot(self) -> dict[str, int]:
        return {key: value for key, value in vars(self).items()}


class GameEventFactory:
    def __init__(self, max_lifetime_seconds: int = 86_400) -> None:
        self.max_lifetime_seconds = max_lifetime_seconds

    def create(self, action: ActionResult, event: Event) -> GameEvent:
        now = action.created_at
        if action.expires_at is not None and action.expires_at <= now:
            raise ValueError("ação expirada não pode virar GameEvent")
        payload = {
            "schema_version": "1.0",
            "command_id": action.action_id,
            "command_type": action.action_type.value,
            "created_at": now.astimezone(timezone.utc).isoformat(),
            "priority": int(action.priority),
            "source_event_id": action.source_event_id,
            "actor": {
                "source_user_id": event.user.external_id,
                "display_name": event.user.display_name,
            },
            "parameters": dict(action.parameters),
            "idempotency_key": f"{action.source_event_id}:{action.rule_id}:{action.action_id}",
            "expires_at": action.expires_at.astimezone(timezone.utc).isoformat() if action.expires_at else None,
        }
        return GameEvent(
            event_id=action.action_id,
            event_type="GAME_COMMAND",
            priority=action.priority,
            timestamp=now,
            payload=payload,
            source_event_id=action.source_event_id,
            expires_at=action.expires_at,
        )


class InteractionRuleEngine:
    def __init__(self, rules: Iterable[InteractionRule], *, max_rules: int = 1_000, max_actions_per_event: int = 8, max_dedupe: int = 20_000) -> None:
        rules = tuple(sorted((rule for rule in rules if rule.enabled), key=lambda item: (int(item.resolved_priority), item.rule_id)))
        if not rules or len(rules) > max_rules:
            raise ValueError(f"rules deve conter entre 1 e {max_rules} regras")
        self.rules = rules
        self.max_actions_per_event = max_actions_per_event
        self.cooldowns = CooldownManager()
        self.rate_limiter = ActionRateLimiter()
        self.aggregator = AggregationManager()
        self.dedupe = TTLSet(max_dedupe, 86_400)
        self.factory = GameEventFactory()
        self.metrics = RuleMetrics()

    def _matches(self, rule: InteractionRule, event: Event) -> bool:
        match = rule.match
        if event.event_type != match.event_type:
            return False
        payload = event.payload
        if match.gift_id is not None and payload.get("gift_id") != match.gift_id:
            return False
        if match.gift_name is not None and str(payload.get("gift_name") or "") != match.gift_name:
            return False
        if match.min_quantity is not None:
            quantity = payload.get("repeat_count", 0)
            if not isinstance(quantity, int) or quantity < match.min_quantity:
                return False
        if match.keyword is not None:
            text = payload.get("text", "")
            if not isinstance(text, str):
                return False
            left = text if match.case_sensitive else text.casefold()
            right = match.keyword if match.case_sensitive else match.keyword.casefold()
            if right not in left:
                return False
        if match.user_external_id is not None and event.user.external_id != match.user_external_id:
            return False
        return True

    def evaluate(self, event: Event) -> list[GameEvent]:
        actions: list[GameEvent] = []
        for rule in self.rules:
            self.metrics.rules_evaluated += 1
            if not self._matches(rule, event):
                continue
            self.metrics.rules_matched += 1
            if self.cooldowns.blocked(rule, event):
                self.metrics.cooldown_hits += 1
                continue
            if not self.aggregator.accept(rule, event):
                continue
            self.cooldowns.mark(rule, event)
            for definition in rule.actions:
                if len(actions) >= self.max_actions_per_event:
                    self.metrics.actions_dropped += 1
                    break
                action_key = f"{event.event_id}:{rule.rule_id}:{definition.action_type.value}:{definition.action_id}"
                if self.dedupe.contains(action_key):
                    self.metrics.dedupe_hits += 1
                    continue
                self.dedupe.add(action_key)
                created_at = datetime.now(timezone.utc)
                duration = definition.params.get("duration_seconds")
                expires_at = created_at + timedelta(seconds=duration) if isinstance(duration, (int, float)) else None
                result = ActionResult(
                    action_id=str(uuid.uuid4()),
                    action_type=definition.action_type,
                    source_event_id=event.event_id,
                    rule_id=rule.rule_id,
                    priority=rule.resolved_priority,
                    created_at=created_at,
                    expires_at=expires_at,
                    parameters=dict(definition.params),
                )
                try:
                    actions.append(self.factory.create(result, event))
                    self.metrics.actions_created += 1
                except ValueError:
                    self.metrics.actions_dropped += 1
            if rule.stop_processing:
                break
        self.metrics.aggregations_created = self.aggregator.created
        self.metrics.dedupe_hits = self.metrics.dedupe_hits
        # Rate limiting is applied to produced actions, preserving P0.
        allowed: list[GameEvent] = []
        for game_event in actions:
            if self.rate_limiter.allow(event, int(game_event.priority)):
                allowed.append(game_event)
            else:
                self.metrics.actions_dropped += 1
        self.metrics.rate_limit_hits = self.rate_limiter.hits
        return allowed

    def is_expired(self, game_event: GameEvent, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        expired = game_event.expires_at is not None and game_event.expires_at <= now
        if expired:
            self.metrics.expired_actions += 1
        return expired
