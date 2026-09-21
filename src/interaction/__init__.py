"""Interaction Rules Engine: eventos normalizados viram GameEvents configuráveis."""

from src.interaction.engine import GameEventFactory, InteractionRuleEngine, RuleMetrics
from src.interaction.consumer import InteractionConsumer
from src.interaction.config import load_rules, load_rules_file
from src.interaction.models import (
    ActionDefinition,
    ActionResult,
    ActionType,
    AggregationSpec,
    CooldownSpec,
    GameEvent,
    InteractionRule,
    RuleMatch,
    rule_from_dict,
)

__all__ = [
    "ActionDefinition",
    "ActionResult",
    "ActionType",
    "AggregationSpec",
    "CooldownSpec",
    "GameEvent",
    "GameEventFactory",
    "InteractionRule",
    "InteractionRuleEngine",
    "InteractionConsumer",
    "load_rules",
    "load_rules_file",
    "RuleMatch",
    "RuleMetrics",
    "rule_from_dict",
]
