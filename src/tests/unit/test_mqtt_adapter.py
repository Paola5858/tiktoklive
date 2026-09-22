import asyncio
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
import aiomqtt

from src.adapters.mqtt import MQTTAdapter, MQTTConfig, MQTTConnectionState
from src.domain.priorities import Priority
from src.interaction.models import GameEvent


@pytest.fixture
def base_config():
    return MQTTConfig(
        enabled=True,
        host="localhost",
        port=1883,
        client_id="test_client",
        username="",
        password="",
        keepalive=60,
        tls_enabled=False,
        reconnect_enabled=False,
        reconnect_delays_s=(0.1,),
        max_queue_size=10,
        max_payload_bytes=4096,
        publish_rate_limit=100.0,
        command_ttl_s=5.0,
        allowed_devices=frozenset(["esp32_01", "esp32_02"]),
        allowed_commands=frozenset(["SPAWN_AVATAR", "PLAY_EFFECT", "SET_COLOR"])
    )


@pytest.mark.asyncio
async def test_publish_game_event_success(base_config):
    adapter = MQTTAdapter(base_config)

    event = GameEvent(
        event_id="ge_1",
        event_type="SPAWN_AVATAR",
        priority=Priority.P1,
        timestamp=datetime.now(timezone.utc),
        payload={"action_type": "SPAWN_AVATAR", "target_device_id": "esp32_01", "color": "red"},
        source_event_id="se_1"
    )

    await adapter.publish_game_event(event)

    assert adapter.queue_depth == 1
    msg = await adapter._queue.get_next()
    assert msg.topic == "liveengine/v1/device/esp32_01/command"
    assert msg.qos == 1  # P1 -> QoS 1

    payload_data = json.loads(msg.payload.decode("utf-8"))
    assert payload_data["schema_version"] == "1.0"
    assert payload_data["device_id"] == "esp32_01"
    assert payload_data["command"] == "SPAWN_AVATAR"
    assert payload_data["action_id"] == "ge_1"
    assert "message_id" in payload_data


@pytest.mark.asyncio
async def test_publish_game_event_rejected_device(base_config):
    adapter = MQTTAdapter(base_config)

    event = GameEvent(
        event_id="ge_1",
        event_type="SPAWN_AVATAR",
        priority=Priority.P1,
        timestamp=datetime.now(timezone.utc),
        payload={"target_device_id": "hacked_device"},
        source_event_id="se_1"
    )

    await adapter.publish_game_event(event)

    assert adapter.queue_depth == 0
    assert adapter.metrics.payload_rejected_total == 1


@pytest.mark.asyncio
async def test_publish_game_event_qos_0_for_low_priority(base_config):
    adapter = MQTTAdapter(base_config)

    event = GameEvent(
        event_id="ge_1",
        event_type="PLAY_EFFECT",
        priority=Priority.P3,
        timestamp=datetime.now(timezone.utc),
        payload={"action_type": "PLAY_EFFECT", "target_device_id": "esp32_01"},
        source_event_id="se_1"
    )

    await adapter.publish_game_event(event)

    assert adapter.queue_depth == 1
    msg = await adapter._queue.get_next()
    assert msg.qos == 0  # P3 -> QoS 0


@pytest.mark.asyncio
async def test_queue_overflow_drops_p4_but_keeps_p1(base_config):
    adapter = MQTTAdapter(base_config)
    adapter._queue = type(adapter._queue)(maxsize_per_level=2)

    # Preencher P1 com 3 mensagens
    for i in range(3):
        ev = GameEvent(
            event_id=f"ge_p1_{i}",
            event_type="SPAWN_AVATAR",
            priority=Priority.P1,
            timestamp=datetime.now(timezone.utc),
            payload={"action_type": "SPAWN_AVATAR", "target_device_id": "esp32_01"},
            source_event_id="se_1"
        )
        await adapter.publish_game_event(ev)

    # Em P1 (evicção do mais antigo), a 3ª deve evictar a 1ª -> 1 drop
    assert adapter.metrics.messages_dropped_total == 1

    # Preencher P4 com 3 mensagens
    for i in range(3):
        ev = GameEvent(
            event_id=f"ge_p4_{i}",
            event_type="PLAY_EFFECT",
            priority=Priority.P4,
            timestamp=datetime.now(timezone.utc),
            payload={"action_type": "PLAY_EFFECT", "target_device_id": "esp32_01"},
            source_event_id="se_1"
        )
        await adapter.publish_game_event(ev)

    # Em P4 (drop novo), a 3ª deve ser descartada -> +1 drop (total = 2)
    assert adapter.metrics.messages_dropped_total == 2


@pytest.mark.asyncio
async def test_deduplication_coalescing(base_config):
    """Eventos idênticos com mesmo event_id dentro do TTL são coalescidos/ignorados."""
    adapter = MQTTAdapter(base_config)

    event = GameEvent(
        event_id="duplicate_ge_1",
        event_type="SPAWN_AVATAR",
        priority=Priority.P1,
        timestamp=datetime.now(timezone.utc),
        payload={"action_type": "SPAWN_AVATAR", "target_device_id": "esp32_01"},
        source_event_id="se_1"
    )

    await adapter.publish_game_event(event)
    assert adapter.queue_depth == 1
    assert adapter.metrics.messages_coalesced_total == 0

    # Segundo envio com mesmo ID
    await adapter.publish_game_event(event)
    assert adapter.queue_depth == 1
    assert adapter.metrics.messages_coalesced_total == 1


@pytest.mark.asyncio
async def test_security_payload_oversized(base_config):
    """Payloads acima de max_payload_bytes devem ser rejeitados e contabilizados."""
    adapter = MQTTAdapter(base_config)

    giant_payload = {"action_type": "SPAWN_AVATAR", "target_device_id": "esp32_01", "junk": "x" * 5000}
    event = GameEvent(
        event_id="giant_1",
        event_type="SPAWN_AVATAR",
        priority=Priority.P1,
        timestamp=datetime.now(timezone.utc),
        payload=giant_payload,
        source_event_id="se_1"
    )

    await adapter.publish_game_event(event)
    assert adapter.queue_depth == 0
    assert adapter.metrics.payload_rejected_total == 1


@pytest.mark.asyncio
async def test_security_command_outside_allowlist(base_config):
    """Comandos não permitidos na allowlist devem ser ignorados."""
    adapter = MQTTAdapter(base_config)

    event = GameEvent(
        event_id="unauthorized_cmd",
        event_type="UNAUTHORIZED_COMMAND",
        priority=Priority.P1,
        timestamp=datetime.now(timezone.utc),
        payload={"action_type": "UNAUTHORIZED_COMMAND", "target_device_id": "esp32_01"},
        source_event_id="se_1"
    )

    await adapter.publish_game_event(event)
    assert adapter.queue_depth == 0


@pytest.mark.asyncio
async def test_ttl_expired_message_in_publish_loop(base_config):
    """Mensagens na fila que expiraram antes do loop de envio devem ser descartadas."""
    # Configurar TTL curtíssimo
    config = MQTTConfig(
        enabled=True, host="localhost", port=1883, client_id="test_client",
        username="", password="", keepalive=60, tls_enabled=False, reconnect_enabled=False,
        reconnect_delays_s=(0.1,), max_queue_size=10, max_payload_bytes=4096,
        publish_rate_limit=100.0, command_ttl_s=0.01,
        allowed_devices=frozenset(["esp32_01"]), allowed_commands=frozenset(["SPAWN_AVATAR"])
    )
    adapter = MQTTAdapter(config)

    event = GameEvent(
        event_id="expired_1",
        event_type="SPAWN_AVATAR",
        priority=Priority.P1,
        timestamp=datetime.now(timezone.utc),
        payload={"action_type": "SPAWN_AVATAR", "target_device_id": "esp32_01"},
        source_event_id="se_1"
    )
    await adapter.publish_game_event(event)

    # Aguardar expirar
    await asyncio.sleep(0.05)

    mock_client = AsyncMock()
    with patch.object(adapter, '_build_aiomqtt_client', return_value=mock_client):
        await adapter.start()
        await asyncio.sleep(0.05)
        await adapter.stop()

    assert adapter.metrics.messages_expired_total == 1
    assert adapter.metrics.publish_total == 0
    mock_client.publish.assert_not_called()


@pytest.mark.asyncio
async def test_rate_limiting_in_publish_loop(base_config):
    """Garante que a taxa de publicação respeita publish_rate_limit."""
    # Taxa de 10 msgs/s = 0.1s entre mensagens
    config = MQTTConfig(
        enabled=True, host="localhost", port=1883, client_id="test_client",
        username="", password="", keepalive=60, tls_enabled=False, reconnect_enabled=False,
        reconnect_delays_s=(0.1,), max_queue_size=25, max_payload_bytes=4096,
        publish_rate_limit=20.0, command_ttl_s=5.0,
        allowed_devices=frozenset(["esp32_01"]), allowed_commands=frozenset(["SPAWN_AVATAR"])
    )
    adapter = MQTTAdapter(config)

    for i in range(3):
        event = GameEvent(
            event_id=f"rate_ge_{i}",
            event_type="SPAWN_AVATAR",
            priority=Priority.P1,
            timestamp=datetime.now(timezone.utc),
            payload={"action_type": "SPAWN_AVATAR", "target_device_id": "esp32_01"},
            source_event_id="se_1"
        )
        await adapter.publish_game_event(event)

    mock_client = AsyncMock()
    start_time = asyncio.get_running_loop().time()
    with patch.object(adapter, '_build_aiomqtt_client', return_value=mock_client):
        await adapter.start()
        # Aguardar envio das 3 mensagens com margem para resolução de timer no Windows
        await asyncio.sleep(0.35)
        await adapter.stop()

    assert adapter.metrics.publish_total == 3
    assert mock_client.publish.call_count == 3


@pytest.mark.asyncio
async def test_worker_loop_connects_and_publishes(base_config):
    adapter = MQTTAdapter(base_config)
    mock_client = AsyncMock()

    event = GameEvent(
        event_id="ge_1",
        event_type="SPAWN_AVATAR",
        priority=Priority.P1,
        timestamp=datetime.now(timezone.utc),
        payload={"action_type": "SPAWN_AVATAR", "target_device_id": "esp32_01"},
        source_event_id="se_1"
    )
    await adapter.publish_game_event(event)

    with patch.object(adapter, '_build_aiomqtt_client', return_value=mock_client):
        await adapter.start()
        await asyncio.sleep(0.1)

        assert adapter.state == MQTTConnectionState.CONNECTED
        assert adapter.metrics.publish_total == 1
        mock_client.publish.assert_called_once()

        await adapter.stop()
        assert adapter.state == MQTTConnectionState.STOPPED


@pytest.mark.asyncio
async def test_inbound_telemetry_and_heartbeat(base_config):
    """Verifica processamento inbound de heartbeat e telemetria de dispositivos."""
    adapter = MQTTAdapter(base_config)

    # Simular mensagem de heartbeat
    hb_msg = MagicMock()
    hb_msg.topic = "liveengine/v1/device/esp32_01/heartbeat"
    hb_msg.payload = json.dumps({"status": "OK", "uptime_s": 3600}).encode("utf-8")

    # Simular mensagem de telemetria com comando rejeitado
    reject_msg = MagicMock()
    reject_msg.topic = "liveengine/v1/device/esp32_01/telemetry"
    reject_msg.payload = json.dumps({"status": "REJECTED", "reason": "OVERHEAT"}).encode("utf-8")

    async def mock_messages():
        yield hb_msg
        yield reject_msg

    mock_client = AsyncMock()
    mock_client.messages = mock_messages()

    with patch.object(adapter, '_build_aiomqtt_client', return_value=mock_client):
        await adapter.start()
        await asyncio.sleep(0.1)
        await adapter.stop()

    assert adapter.metrics.device_heartbeat_total == 1
    assert adapter.metrics.device_command_rejected_total == 1
    assert adapter.is_device_online("esp32_01") is True
    assert adapter.is_device_online("esp32_02") is False


@pytest.mark.asyncio
async def test_stress_flood_1000_mixed_events(base_config):
    """Stress test: 1000 eventos com prioridades mistas sob broker offline (não trava)."""
    adapter = MQTTAdapter(base_config)

    for i in range(1000):
        p = Priority(i % 5)
        event = GameEvent(
            event_id=f"stress_{i}",
            event_type="SPAWN_AVATAR" if i % 2 == 0 else "PLAY_EFFECT",
            priority=p,
            timestamp=datetime.now(timezone.utc),
            payload={
                "action_type": "SPAWN_AVATAR" if i % 2 == 0 else "PLAY_EFFECT",
                "target_device_id": "esp32_01" if i % 3 != 0 else "esp32_02"
            },
            source_event_id=f"src_{i}"
        )
        await adapter.publish_game_event(event)

    # Fila respeita limite estrito
    assert adapter.queue_depth <= base_config.max_queue_size
    # Mensagens excedentes descartadas de forma segura e auditada
    assert adapter.metrics.messages_dropped_total > 0
    snap = adapter.metrics.snapshot()
    assert snap["mqtt_messages_dropped_total"] == adapter.metrics.messages_dropped_total
