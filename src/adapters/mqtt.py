"""MQTT Adapter — transporte desacoplado de GameEvents para dispositivos físicos.

Integra o Live Engine a brokers MQTT (ex: Mosquitto) para atuação em ESP32/IoT.
Biblioteca: aiomqtt 2.5.1 (com paho-mqtt 2.1.0).

PRINCÍPIO CENTRAL: MQTT é um consumidor. Falhas não afetam TikTok ou Roblox.
A arquitetura consome GameEvents produzidos pelo Interaction Rule Engine.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any

import aiomqtt
import paho.mqtt.client as paho_mqtt

from src.domain.priorities import Priority
from src.interaction.models import GameEvent
from src.logging import get_logger
from src.observability.health import ComponentHealth, Watchdog

LOGGER = get_logger(__name__)

# Constantes de Segurança / Limites
_MAX_PAYLOAD_BYTES_DEFAULT = 4096
_MAX_QUEUE_SIZE_DEFAULT = 500


class MQTTConnectionState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class MQTTConfig:
    enabled: bool
    host: str
    port: int
    client_id: str
    username: str
    password: str
    keepalive: int
    tls_enabled: bool
    reconnect_enabled: bool
    reconnect_delays_s: tuple[float, ...]
    max_queue_size: int
    max_payload_bytes: int
    publish_rate_limit: float  # publicações por segundo global
    command_ttl_s: float
    allowed_devices: frozenset[str]
    allowed_commands: frozenset[str]

    def __post_init__(self) -> None:
        if self.enabled and not self.host:
            raise ValueError("MQTT_BROKER_HOST inválido")
        if not (1 <= self.port <= 65535):
            raise ValueError(f"MQTT port inválido: {self.port}")
        if self.max_queue_size <= 0:
            raise ValueError("max_queue_size deve ser positivo")
        if self.max_payload_bytes <= 0:
            raise ValueError("max_payload_bytes deve ser positivo")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "MQTTConfig":
        source = env if env is not None else os.environ

        enabled_raw = source.get("MQTT_ENABLED", "false").strip().lower()
        enabled = enabled_raw in ("1", "true", "yes", "on")

        host = source.get("MQTT_BROKER_HOST", "localhost").strip()
        port = int(source.get("MQTT_BROKER_PORT", "1883"))
        client_id = source.get("MQTT_CLIENT_ID", f"liveengine-{uuid.uuid4().hex[:8]}").strip()
        username = source.get("MQTT_USERNAME", "").strip()
        password = source.get("MQTT_PASSWORD", "")
        keepalive = int(source.get("MQTT_KEEPALIVE", "60"))

        tls_raw = source.get("MQTT_TLS_ENABLED", "false").strip().lower()
        tls_enabled = tls_raw in ("1", "true", "yes", "on")

        rec_raw = source.get("MQTT_RECONNECT_ENABLED", "true").strip().lower()
        reconnect_enabled = rec_raw in ("1", "true", "yes", "on")

        delays_raw = source.get("MQTT_RECONNECT_DELAYS", "2,4,8,16,30").strip()
        delays = tuple(float(x.strip()) for x in delays_raw.split(",") if x.strip())

        max_queue = int(source.get("MQTT_MAX_QUEUE_SIZE", str(_MAX_QUEUE_SIZE_DEFAULT)))
        max_payload = int(source.get("MQTT_MAX_PAYLOAD_BYTES", str(_MAX_PAYLOAD_BYTES_DEFAULT)))
        rate_limit = float(source.get("MQTT_PUBLISH_RATE_LIMIT", "10.0"))
        ttl = float(source.get("MQTT_COMMAND_TTL", "30.0"))

        devices_raw = source.get("MQTT_ALLOWED_DEVICES", "").strip()
        allowed_devices = frozenset(d.strip() for d in devices_raw.split(",") if d.strip())

        commands_raw = source.get("MQTT_ALLOWED_COMMANDS", "SPAWN_AVATAR,PLAY_EFFECT").strip()
        allowed_commands = frozenset(c.strip() for c in commands_raw.split(",") if c.strip())

        return cls(
            enabled=enabled,
            host=host,
            port=port,
            client_id=client_id,
            username=username,
            password=password,
            keepalive=keepalive,
            tls_enabled=tls_enabled,
            reconnect_enabled=reconnect_enabled,
            reconnect_delays_s=delays,
            max_queue_size=max_queue,
            max_payload_bytes=max_payload,
            publish_rate_limit=rate_limit,
            command_ttl_s=ttl,
            allowed_devices=allowed_devices,
            allowed_commands=allowed_commands,
        )

    @classmethod
    def disabled(cls) -> "MQTTConfig":
        return cls(
            enabled=False, host="localhost", port=1883, client_id="",
            username="", password="", keepalive=60, tls_enabled=False,
            reconnect_enabled=True, reconnect_delays_s=(2,4,8,16,30),
            max_queue_size=100, max_payload_bytes=4096, publish_rate_limit=10.0,
            command_ttl_s=30.0, allowed_devices=frozenset(), allowed_commands=frozenset()
        )


@dataclass
class MQTTMetrics:
    connect_attempts_total: int = 0
    reconnects_total: int = 0
    publish_total: int = 0
    publish_failures_total: int = 0
    messages_dropped_total: int = 0
    messages_coalesced_total: int = 0
    messages_expired_total: int = 0
    payload_rejected_total: int = 0
    device_heartbeat_total: int = 0
    device_command_rejected_total: int = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "mqtt_connect_attempts_total": self.connect_attempts_total,
            "mqtt_reconnect_total": self.reconnects_total,
            "mqtt_publish_total": self.publish_total,
            "mqtt_publish_failures_total": self.publish_failures_total,
            "mqtt_messages_dropped_total": self.messages_dropped_total,
            "mqtt_messages_coalesced_total": self.messages_coalesced_total,
            "mqtt_messages_expired_total": self.messages_expired_total,
            "mqtt_payload_rejected_total": self.payload_rejected_total,
            "device_heartbeat_total": self.device_heartbeat_total,
            "device_command_rejected_total": self.device_command_rejected_total,
        }


@dataclass
class MQTTMessage:
    """Mensagem envelopada na fila para publicação."""
    topic: str
    payload: bytes
    qos: int
    priority: int
    created_at: float
    expires_at: float
    retain: bool = False

    def is_expired(self) -> bool:
        return asyncio.get_running_loop().time() >= self.expires_at


class MQTTPriorityQueue:
    """Fila MQTT limitada, priorizada.
    P0/P1 não são expulsos por overflow de P4.
    """
    def __init__(self, maxsize_per_level: int):
        self._queues: list[asyncio.Queue[MQTTMessage]] = [
            asyncio.Queue(maxsize=max(1, maxsize_per_level)) for _ in range(5)
        ]
        self._dropped = 0
        self._queued = 0

    def depth(self) -> int:
        return sum(q.qsize() for q in self._queues)

    def put_nowait(self, msg: MQTTMessage) -> tuple[bool, bool]:
        """Adiciona mensagem à fila por prioridade.
        Retorna: (aceito: bool, descartou_antigo: bool)
        """
        p = max(0, min(4, msg.priority))
        q = self._queues[p]
        if not q.full():
            q.put_nowait(msg)
            self._queued += 1
            return True, False
        else:
            if p <= 1:
                try:
                    q.get_nowait()
                    self._dropped += 1
                    q.put_nowait(msg)
                    self._queued += 1
                    return True, True
                except asyncio.QueueEmpty:
                    pass
            self._dropped += 1
            return False, False

    def dropped_count(self) -> int:
        return self._dropped

    async def get_next(self) -> MQTTMessage:
        while True:
            for q in self._queues:
                if not q.empty():
                    try:
                        return q.get_nowait()
                    except asyncio.QueueEmpty:
                        continue
            await asyncio.sleep(0.02)


class MQTTAdapter:
    def __init__(
        self,
        config: MQTTConfig | None = None,
        watchdog: Watchdog | None = None,
    ) -> None:
        self._config = config or MQTTConfig.disabled()
        self._state = MQTTConnectionState.DISCONNECTED
        self._metrics = MQTTMetrics()
        self._queue = MQTTPriorityQueue(
            maxsize_per_level=max(1, self._config.max_queue_size // 5)
        )

        self._health: ComponentHealth | None = None
        if watchdog and self._config.enabled:
            self._health = watchdog.register("MQTTAdapter")

        self._client: aiomqtt.Client | None = None
        self._stop_event = asyncio.Event()
        self._worker_task: asyncio.Task[None] | None = None
        self._dedup_cache: dict[str, float] = {}
        self._device_last_seen: dict[str, datetime] = {}

    @property
    def metrics(self) -> MQTTMetrics:
        return self._metrics

    @property
    def queue_depth(self) -> int:
        return self._queue.depth()

    @property
    def state(self) -> MQTTConnectionState:
        return self._state

    @property
    def device_last_seen(self) -> dict[str, datetime]:
        return dict(self._device_last_seen)

    def is_device_online(self, device_id: str, timeout_s: float = 60.0) -> bool:
        last = self._device_last_seen.get(device_id)
        if not last:
            return False
        return (datetime.now(timezone.utc) - last).total_seconds() <= timeout_s

    def _build_aiomqtt_client(self) -> aiomqtt.Client:
        tls_params = aiomqtt.TLSParameters() if self._config.tls_enabled else None

        return aiomqtt.Client(
            hostname=self._config.host,
            port=self._config.port,
            username=self._config.username or None,
            password=self._config.password or None,
            client_id=self._config.client_id,
            keepalive=self._config.keepalive,
            tls_params=tls_params,
            clean_session=True,
        )

    async def publish_game_event(self, game_event: GameEvent) -> None:
        """Consome GameEvents, roteia para tópicos e enfileira para envio."""
        if not self._config.enabled:
            return

        # Deduplicação baseada em event_id com TTL
        now_mono = asyncio.get_running_loop().time()
        if len(self._dedup_cache) > 1000:
            self._dedup_cache = {k: exp for k, exp in self._dedup_cache.items() if exp > now_mono}

        if game_event.event_id in self._dedup_cache:
            LOGGER.debug("MQTTAdapter: evento duplicado ignorado (dedup) %s", game_event.event_id)
            self._metrics.messages_coalesced_total += 1
            return

        command = game_event.payload.get("action_type") or game_event.event_type
        if self._config.allowed_commands and command not in self._config.allowed_commands:
            return

        device_id = game_event.payload.get("target_device_id")
        if not device_id:
            return

        if self._config.allowed_devices and device_id not in self._config.allowed_devices:
            self._metrics.payload_rejected_total += 1
            LOGGER.warning("MQTTAdapter: device_id rejeitado: %s", device_id)
            return

        topic = f"liveengine/v1/device/{device_id}/command"

        now = datetime.now(timezone.utc)
        expires = game_event.expires_at or (now + timedelta(seconds=self._config.command_ttl_s))

        # Registro no cache de deduplicação
        self._dedup_cache[game_event.event_id] = now_mono + self._config.command_ttl_s

        msg_dict = {
            "schema_version": "1.0",
            "message_id": str(uuid.uuid4()),
            "event_id": game_event.source_event_id,
            "action_id": game_event.event_id,
            "device_id": device_id,
            "command": command,
            "timestamp": now.isoformat(),
            "expires_at": expires.isoformat(),
            "payload": game_event.payload,
        }

        try:
            payload_bytes = json.dumps(msg_dict, separators=(",", ":")).encode("utf-8")
        except Exception:
            self._metrics.payload_rejected_total += 1
            return

        if len(payload_bytes) > self._config.max_payload_bytes:
            self._metrics.payload_rejected_total += 1
            LOGGER.warning("MQTTAdapter: payload excede limite de bytes (%d)", len(payload_bytes))
            return

        qos = 1 if game_event.priority in (Priority.P0, Priority.P1) else 0

        msg = MQTTMessage(
            topic=topic,
            payload=payload_bytes,
            qos=qos,
            priority=int(game_event.priority),
            created_at=asyncio.get_running_loop().time(),
            expires_at=asyncio.get_running_loop().time() + self._config.command_ttl_s,
            retain=False,
        )

        accepted, evicted = self._queue.put_nowait(msg)
        if not accepted or evicted:
            self._metrics.messages_dropped_total += 1

    async def start(self) -> None:
        if not self._config.enabled:
            self._state = MQTTConnectionState.STOPPED
            return

        self._stop_event.clear()
        self._worker_task = asyncio.create_task(self._run_loop(), name="mqtt_worker")
        LOGGER.info("MQTTAdapter: iniciado (host=%s)", self._config.host)

    async def stop(self) -> None:
        if not self._config.enabled:
            return

        LOGGER.info("MQTTAdapter: parando")
        self._state = MQTTConnectionState.STOPPING
        self._stop_event.set()

        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        self._state = MQTTConnectionState.STOPPED
        LOGGER.info("MQTTAdapter: parado")

    async def _run_loop(self) -> None:
        first = True
        delays = self._config.reconnect_delays_s

        while not self._stop_event.is_set():
            if not first:
                self._state = MQTTConnectionState.RECONNECTING
                self._metrics.reconnects_total += 1
            first = False

            self._metrics.connect_attempts_total += 1
            self._state = MQTTConnectionState.CONNECTING

            try:
                self._client = self._build_aiomqtt_client()
                async with self._client:
                    self._state = MQTTConnectionState.CONNECTED
                    if self._health:
                        self._health.record_success()
                    LOGGER.info("MQTTAdapter: conectado ao broker %s", self._config.host)

                    # Subscrição a tópicos de telemetria e heartbeat de dispositivos
                    try:
                        await self._client.subscribe("liveengine/v1/device/+/heartbeat")
                        await self._client.subscribe("liveengine/v1/device/+/telemetry")
                    except Exception as sub_err:
                        LOGGER.warning("MQTTAdapter: falha ao subscrever tópicos de telemetria: %s", sub_err)

                    publish_task = asyncio.create_task(self._publish_loop())
                    telemetry_task = asyncio.create_task(self._telemetry_loop())
                    stop_task = asyncio.create_task(self._stop_event.wait())

                    done, pending = await asyncio.wait(
                        [publish_task, telemetry_task, stop_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    for t in pending:
                        t.cancel()

                    if stop_task in done:
                        return

            except aiomqtt.MqttError as e:
                self._state = MQTTConnectionState.FAILED
                if self._health:
                    self._health.report_degraded(f"MQTT Error: {str(e)}")
                LOGGER.warning("MQTTAdapter: erro na conexão: %s", e)
            except Exception as e:
                self._state = MQTTConnectionState.FAILED
                LOGGER.exception("MQTTAdapter: falha crítica")

            if not self._config.reconnect_enabled:
                LOGGER.info("MQTTAdapter: reconexão desabilitada, parando loop")
                break

            attempt = min(self._metrics.connect_attempts_total - 1, len(delays) - 1)
            delay = delays[attempt]
            LOGGER.info("MQTTAdapter: reconnect em %.0fs", delay)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                pass

    async def _publish_loop(self) -> None:
        last_publish_time = 0.0
        min_interval = (
            1.0 / self._config.publish_rate_limit
            if self._config.publish_rate_limit > 0
            else 0.0
        )

        while True:
            msg = await self._queue.get_next()
            if msg.is_expired():
                self._metrics.messages_expired_total += 1
                continue

            # Aplicação real de rate limit
            if min_interval > 0:
                elapsed = asyncio.get_running_loop().time() - last_publish_time
                if elapsed < min_interval:
                    await asyncio.sleep(min_interval - elapsed)

            try:
                if self._client:
                    await self._client.publish(
                        msg.topic, payload=msg.payload, qos=msg.qos, retain=msg.retain
                    )
                    last_publish_time = asyncio.get_running_loop().time()
                    self._metrics.publish_total += 1
                    if self._health:
                        self._health.record_success()
            except aiomqtt.MqttError as e:
                self._metrics.publish_failures_total += 1
                LOGGER.error("MQTTAdapter: falha ao publicar: %s", e)
                raise

    async def _telemetry_loop(self) -> None:
        """Processa mensagens inbound de telemetria e heartbeat dos dispositivos."""
        if not self._client:
            return

        try:
            # Compatibilidade com aiomqtt.Client.messages
            messages_iter = getattr(self._client, "messages", None)
            if messages_iter is None:
                await self._stop_event.wait()
                return

            async for message in messages_iter:
                if self._stop_event.is_set():
                    break
                try:
                    topic_str = str(message.topic)
                    parts = topic_str.split("/")
                    if len(parts) >= 5 and parts[0] == "liveengine" and parts[2] == "device":
                        device_id = parts[3]
                        msg_type = parts[4]

                        if self._config.allowed_devices and device_id not in self._config.allowed_devices:
                            LOGGER.warning("MQTTAdapter: telemetria de device_id não autorizado: %s", device_id)
                            continue

                        raw_payload = message.payload
                        if isinstance(raw_payload, (bytes, bytearray)):
                            if len(raw_payload) > self._config.max_payload_bytes:
                                continue
                            data = json.loads(raw_payload.decode("utf-8"))
                        elif isinstance(raw_payload, str):
                            data = json.loads(raw_payload)
                        else:
                            continue

                        now_utc = datetime.now(timezone.utc)
                        self._device_last_seen[device_id] = now_utc

                        if msg_type == "heartbeat":
                            self._metrics.device_heartbeat_total += 1
                        elif msg_type == "telemetry":
                            if data.get("status") == "REJECTED" or "command_rejected" in data:
                                self._metrics.device_command_rejected_total += 1
                except Exception as inner_err:
                    LOGGER.debug("MQTTAdapter: falha ao ler telemetria individual: %s", inner_err)

            await self._stop_event.wait()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            LOGGER.warning("MQTTAdapter: erro no telemetry loop: %s", e)
