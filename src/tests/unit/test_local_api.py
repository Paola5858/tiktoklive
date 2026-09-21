"""Testes da Local API: health, events (cursor/limit), ack, validação de payload."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.adapters.local_api import create_app
from src.adapters.roblox import RobloxBridge, RobloxBridgeConfig
from src.domain.events import Event, EventType, EventUser


def _event(**kwargs) -> Event:
    defaults: dict = {
        "event_type": EventType.COMMENT,
        "source": "tiktok",
        "user": EventUser(display_name="joao"),
        "payload": {"text": "oi"},
    }
    defaults.update(kwargs)
    return Event(**defaults)


@pytest.fixture
def bridge() -> RobloxBridge:
    return RobloxBridge(RobloxBridgeConfig(buffer_capacity=50))


@pytest.fixture
def client(bridge: RobloxBridge) -> TestClient:
    app = create_app(bridge)
    return TestClient(app)


def test_health_returns_ok_and_no_event_payload(client: TestClient):
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "bridge" in body
    # Health não é o lugar de eventos — contrato explícito da fase 1 e 4.
    assert "events" not in body


def test_events_endpoint_returns_empty_when_bridge_is_empty(client: TestClient):
    response = client.get("/events")

    assert response.status_code == 200
    body = response.json()
    assert body["events"] == []
    assert body["cursor"] == 0
    assert body["gap_detected"] is False
    assert body["schema_version"] == "1.0"


@pytest.mark.asyncio
async def test_events_endpoint_returns_buffered_events(bridge: RobloxBridge, client: TestClient):
    await bridge.handle(_event())
    await bridge.handle(_event(payload={"text": "segundo"}))

    response = client.get("/events")

    body = response.json()
    assert len(body["events"]) == 2
    assert body["cursor"] == 2


@pytest.mark.asyncio
async def test_events_endpoint_respects_since_cursor(bridge: RobloxBridge, client: TestClient):
    await bridge.handle(_event())
    await bridge.handle(_event())

    response = client.get("/events", params={"since": 1})

    body = response.json()
    assert len(body["events"]) == 1
    assert body["events"][0]["sequence_number"] == 2


def test_events_endpoint_rejects_negative_since(client: TestClient):
    response = client.get("/events", params={"since": -1})
    assert response.status_code == 422  # validação do FastAPI/Pydantic


def test_events_endpoint_rejects_limit_above_max(client: TestClient):
    response = client.get("/events", params={"limit": 99999})
    assert response.status_code == 422


def test_events_endpoint_rejects_limit_below_min(client: TestClient):
    response = client.get("/events", params={"limit": 0})
    assert response.status_code == 422


def test_ack_endpoint_advances_marker(client: TestClient):
    response = client.post("/ack", json={"up_to_sequence": 5})

    assert response.status_code == 200
    assert response.json()["acknowledged_up_to"] == 5


def test_ack_endpoint_rejects_missing_field(client: TestClient):
    response = client.post("/ack", json={})
    assert response.status_code == 422


def test_ack_endpoint_rejects_negative_sequence(client: TestClient):
    response = client.post("/ack", json={"up_to_sequence": -1})
    assert response.status_code == 422


def test_ack_endpoint_rejects_wrong_type(client: TestClient):
    response = client.post("/ack", json={"up_to_sequence": "not-a-number"})
    assert response.status_code == 422


def test_unknown_route_returns_404(client: TestClient):
    response = client.get("/does-not-exist")
    assert response.status_code == 404


def test_events_endpoint_rejects_malformed_query_gracefully(client: TestClient):
    # "since" não numérico não deve derrubar o processo, só rejeitar.
    response = client.get("/events", params={"since": "not-a-number"})
    assert response.status_code == 422
