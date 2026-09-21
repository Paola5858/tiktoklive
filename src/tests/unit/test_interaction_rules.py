from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.domain.events import Event, EventType, EventUser
from src.domain.priorities import Priority
from src.interaction import InteractionRuleEngine, ActionType, rule_from_dict


def event(event_type=EventType.GIFT, payload=None, event_id="event-1", user_id="u-1"):
    return Event(
        event_type=event_type,
        source="tiktok",
        user=EventUser("viewer", user_id),
        payload=payload or {"gift_id": 101, "gift_name": "fixture", "repeat_count": 1},
        event_id=event_id,
    )


def rule(**overrides):
    raw = {
        "id": "gift-rule",
        "enabled": True,
        "event_type": "GIFT",
        "match": {"gift_id": 101},
        "actions": [{"type": "SPAWN_AVATAR", "params": {"duration_seconds": 10, "effect": "HEARTS"}}],
        "priority": "P1",
    }
    raw.update(overrides)
    return rule_from_dict(raw)


def test_rule_matches_stable_gift_id_and_factory_preserves_source():
    result = InteractionRuleEngine([rule()]).evaluate(event())
    assert len(result) == 1
    assert result[0].event_type == "GAME_COMMAND"
    assert result[0].payload["command_type"] == "SPAWN_AVATAR"
    assert result[0].payload["source_event_id"] == "event-1"
    assert result[0].priority == Priority.P1


def test_multiple_actions_are_deterministic_and_bounded():
    current = rule(actions=[
        {"type": "SPAWN_AVATAR", "params": {"duration_seconds": 10}},
        {"type": "SPAWN_AVATAR", "params": {"duration_seconds": 10, "effect": "GOLD_AURA"}},
    ])
    results = InteractionRuleEngine([current]).evaluate(event())
    assert [item.payload["command_type"] for item in results] == ["SPAWN_AVATAR", "SPAWN_AVATAR"]


def test_cooldown_per_user_blocks_repeated_gift_but_allows_other_user():
    engine = InteractionRuleEngine([rule(cooldown={"scope": "user", "seconds": 30})])
    assert len(engine.evaluate(event(event_id="a", user_id="same"))) == 1
    assert len(engine.evaluate(event(event_id="b", user_id="same"))) == 0
    assert len(engine.evaluate(event(event_id="c", user_id="other"))) == 1
    assert engine.metrics.cooldown_hits == 1


def test_same_event_id_is_deduplicated_without_merging_different_events():
    engine = InteractionRuleEngine([rule(cooldown={"seconds": 0})])
    assert len(engine.evaluate(event(event_id="same"))) == 1
    assert len(engine.evaluate(event(event_id="same"))) == 0
    assert len(engine.evaluate(event(event_id="different"))) == 1
    assert engine.metrics.dedupe_hits == 1


def test_comment_keyword_is_case_insensitive_when_configured():
    comment_rule = rule(
        id="keyword",
        event_type="COMMENT",
        match={"keyword": "roblox", "case_sensitive": False},
        actions=[{"type": "SPAWN_AVATAR", "params": {"duration_seconds": 5}}],
        priority="P3",
    )
    engine = InteractionRuleEngine([comment_rule])
    result = engine.evaluate(event(EventType.COMMENT, {"text": "ROBloX agora"}))
    assert len(result) == 1


def test_aggregation_requires_threshold_before_action():
    aggregate_rule = rule(
        aggregation={"window_seconds": 10, "threshold": 3, "key": "gift_id"},
    )
    engine = InteractionRuleEngine([aggregate_rule])
    assert engine.evaluate(event(event_id="1")) == []
    assert engine.evaluate(event(event_id="2")) == []
    assert len(engine.evaluate(event(event_id="3"))) == 1
    assert engine.metrics.aggregations_created == 1


def test_rate_limit_drops_low_priority_burst_but_keeps_p0():
    low = rule(priority="P3", id="low")
    engine = InteractionRuleEngine([low])
    engine.rate_limiter.global_limit = 1
    assert len(engine.evaluate(event(event_id="a"))) == 1
    assert len(engine.evaluate(event(event_id="b"))) == 0
    assert engine.metrics.rate_limit_hits == 1


def test_invalid_actions_and_unbounded_values_fail_early():
    with pytest.raises(ValueError):
        rule(actions=[{"type": "SHOW_MESSAGE", "params": {}}])
    with pytest.raises(ValueError):
        rule(actions=[{"type": "SPAWN_AVATAR", "params": {"duration_seconds": -1}}])
    with pytest.raises(ValueError):
        rule(cooldown={"scope": "global", "seconds": 100000})


def test_expiration_is_reported():
    game_event = InteractionRuleEngine([rule()]).evaluate(event())[0]
    assert game_event.expires_at is not None
    assert InteractionRuleEngine([rule()]).is_expired(game_event, datetime.now(timezone.utc)) is False
