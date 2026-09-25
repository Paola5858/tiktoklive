from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from src.adapters.local_api import create_app
from src.adapters.roblox import RobloxBridge, RobloxBridgeConfig
from src.dashboard_api import _read_recent_logs, load_rules_for_dashboard


def _rules_file(tmp_path: Path) -> Path:
    path = tmp_path / "rules.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "rules": [
                    {
                        "id": "gift-test",
                        "enabled": True,
                        "event_type": "GIFT",
                        "match": {"gift_name": "example_gift"},
                        "actions": [{"type": "SPAWN_AVATAR", "params": {"effect": "HEARTS"}}],
                        "priority": "P1",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_dashboard_serves_same_local_api_and_assets(tmp_path):
    bridge = RobloxBridge(RobloxBridgeConfig.default())
    rules = _rules_file(tmp_path)
    app = create_app(
        bridge,
        capabilities={"roblox_bridge": "ready"},
        dashboard_provider=lambda: {
            "snapshot": {"status": "healthy"},
            "capabilities": {"roblox_bridge": "ready"},
            "integrations": {},
            "events": [],
            "event_cursor": 0,
            "gap_detected": False,
            "limits": {"events": 50, "logs": 200},
        },
        rules_path=str(rules),
        audit_log_dir=str(tmp_path / "logs"),
    )
    client = TestClient(app)
    assert client.get("/dashboard").status_code == 200
    assert "O que está acontecendo agora?" in client.get("/dashboard").text
    assert client.get("/dashboard/assets/styles.css").status_code == 200
    assert client.get("/dashboard/assets/app.js").status_code == 200
    snapshot = client.get("/api/dashboard/snapshot")
    assert snapshot.status_code == 200
    assert snapshot.json()["snapshot"]["status"] == "healthy"
    assert bridge.health_snapshot()["last_poll_at"] is None
    rules_response = client.get("/api/dashboard/rules")
    assert rules_response.status_code == 200
    assert rules_response.json()["count"] == 1


def test_dashboard_logs_are_bounded_and_redacted(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "2026-09-25.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "timestamp": "2026-09-25T21:00:00Z",
                    "level": "ERROR",
                    "component": "TikTokConnector",
                    "message": "falha transitória",
                    "password": "não pode sair",
                }
            )
            for _ in range(250)
        ),
        encoding="utf-8",
    )
    rows = _read_recent_logs(log_dir, limit=200)
    assert len(rows) == 200
    assert rows[0]["password"] == "[redacted]"


def test_rules_loader_rejects_invalid_file(tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps({"schema_version": "9.0", "rules": []}), encoding="utf-8")
    try:
        load_rules_for_dashboard(path)
    except ValueError as exc:
        assert "schema_version" in str(exc)
    else:
        raise AssertionError("regra incompatível deveria falhar")
