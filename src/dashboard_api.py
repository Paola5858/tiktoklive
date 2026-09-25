"""Borda de dados do dashboard operacional.

A UI não calcula saúde, throughput ou latência. Este módulo apenas compõe
contratos já existentes e limita dados de leitura para impedir crescimento
infinito no navegador.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from src.adapters.roblox import RobloxBridge
from src.interaction.config import load_rules_file

MAX_LOG_LINES = 200
MAX_EVENT_LIMIT = 50
_SECRET_WORDS = ("password", "token", "secret", "api_key", "apikey")


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[redacted]" if any(word in key.lower() for word in _SECRET_WORDS) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _read_recent_logs(log_dir: str | Path, *, limit: int, level: str = "", component: str = "", query: str = "") -> list[dict[str, Any]]:
    root = Path(log_dir)
    files = sorted(root.glob("*.jsonl"), reverse=True)[:3]
    rows: list[dict[str, Any]] = []
    needle = query.strip().lower()
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-MAX_LOG_LINES:]
        except OSError:
            continue
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            row = _redact(row)
            if level and str(row.get("level", "")).upper() != level.upper():
                continue
            if component and component.lower() not in str(row.get("component", "")).lower():
                continue
            if needle and needle not in json.dumps(row, ensure_ascii=False).lower():
                continue
            rows.append(row)
            if len(rows) >= min(max(limit, 1), MAX_LOG_LINES):
                return rows
    return rows


def build_dashboard_payload(
    *,
    bridge: RobloxBridge,
    snapshot_provider: Callable[[], dict[str, Any]],
    capabilities_provider: Callable[[], dict[str, Any]],
    integration_provider: Callable[[], dict[str, Any]],
    rules_path: str | Path,
) -> dict[str, Any]:
    """Monta o único payload operacional consumido pela interface."""
    snapshot = snapshot_provider()
    events, cursor, gap = bridge.peek_recent(MAX_EVENT_LIMIT)
    return {
        "snapshot": snapshot,
        "capabilities": capabilities_provider(),
        "integrations": integration_provider(),
        "events": [event.to_dict() for event in events],
        "event_cursor": cursor,
        "gap_detected": gap,
        "limits": {"events": MAX_EVENT_LIMIT, "logs": MAX_LOG_LINES},
        "rules_path": str(rules_path),
    }


def load_rules_for_dashboard(path: str | Path) -> dict[str, Any]:
    """Retorna regras originais somente para leitura e também valida o arquivo."""
    file_path = Path(path)
    data = json.loads(file_path.read_text(encoding="utf-8"))
    rules = load_rules_file(file_path)
    return {
        "schema_version": data.get("schema_version"),
        "count": len(rules),
        "rules": _redact(data.get("rules", [])),
        "path": str(file_path),
    }
