"""Carregamento explícito e validado das regras declarativas."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from src.interaction.models import InteractionRule, rule_from_dict

RULES_SCHEMA_VERSION = "1.0"
MAX_RULES = 1_000


def load_rules(data: Mapping[str, Any]) -> tuple[InteractionRule, ...]:
    if not isinstance(data, Mapping):
        raise ValueError("configuração de regras deve ser um objeto")
    if data.get("schema_version") != RULES_SCHEMA_VERSION:
        raise ValueError("schema_version de regras incompatível")
    raw_rules = data.get("rules")
    if not isinstance(raw_rules, list) or not 1 <= len(raw_rules) <= MAX_RULES:
        raise ValueError(f"rules deve ser uma lista de 1 a {MAX_RULES} itens")
    rules = tuple(rule_from_dict(raw) for raw in raw_rules)
    ids = [rule.rule_id for rule in rules]
    if len(ids) != len(set(ids)):
        raise ValueError("rule ids devem ser únicos")
    return rules


def load_rules_file(path: str | Path) -> tuple[InteractionRule, ...]:
    file_path = Path(path)
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"não foi possível ler configuração de regras: {file_path}") from exc
    return load_rules(data)
