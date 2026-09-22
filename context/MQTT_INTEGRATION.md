# Phase 9 — MQTT Integration & ESP32 / Physical Actuation

## Architecture Overview

The MQTT Integration module operates as a **decoupled, non-blocking consumer and telemetry processor** (`MQTTAdapter`), interfacing the Live Engine with physical IoT devices (such as ESP32 microcontrollers and safe actuators).

```
TikTok Event → Normalizer → Event Engine → Priority Queue
   → Dispatcher.dispatch(event)
       ├── InteractionConsumer → InteractionRuleEngine → RobloxBridge
       ├── OBSAdapter.handle(event) (OBS Automation)
       └── InteractionConsumer.bridges → MQTTAdapter.publish_game_event(game_event)
               ↓ (non-blocking: validates, serializes, deduplicates, enqueues)
           MQTTPriorityQueue (bounded 5-priority levels, eviction for P0/P1, drop for P2-P4)
               ↓ (isolated background worker task with publish rate limiting)
           MQTTAdapter._publish_loop (aiomqtt 2.3+ / paho-mqtt 2.1+)
               ↓ (MQTT 3.1.1 / 5.0, TLS optional)
           MQTT Broker (e.g. Mosquitto, HiveMQ, EMQX)
               ↓ Topic: liveengine/v1/device/{device_id}/command
           ESP32 Microcontroller / Actuators (LEDs, visual indicators, safe relays)
               ↑ Topic: liveengine/v1/device/{device_id}/heartbeat & telemetry
           MQTTAdapter._telemetry_loop (device health, command rejection, watchdog tracking)
```

### Key Architectural Guarantees
1. **Zero Impact on Ingestion & Other Consumers:** MQTT failure, network latency, or an offline ESP32 never slows down or crashes TikTok ingestion, Roblox, or OBS.
2. **Strict Physical Safety:** Only safe, low-voltage visual/auditory actuators are in scope (LEDs, LED matrices, buzzers). High voltage, thermal, or kinetic hazards are strictly forbidden.
3. **Defense-in-Depth Allowlists:** Device IDs and commands must belong to configured allowlists (`MQTT_ALLOWED_DEVICES` and `MQTT_ALLOWED_COMMANDS`). Arbitrary user comments or metadata can never form dynamic topics or invoke raw physical hardware operations.
4. **Idempotency & Deduplication:** Outbound messages carry UUID `message_id`, `event_id`, and `action_id`. In-memory deduplication cache with TTL prevents replay storms during high-frequency gift combos.
5. **Bounded Resources & Backpressure:** The adapter uses a 5-level priority queue with strict bounds. P0/P1 commands evict older messages of the same priority when full; P2–P4 commands are safely dropped to prevent broker saturation.

---

## Library & Broker Specification

| Property | Value | Notes |
|---|---|---|
| Client Library | `aiomqtt ≥ 2.3.0` | AsyncIO wrapper for Paho MQTT |
| Core MQTT Engine | `paho-mqtt ≥ 2.1.0` | High-performance asynchronous client |
| MQTT Protocols | MQTT v3.1.1 / MQTT v5.0 | Clean session enabled |
| Broker Support | Mosquitto, EMQX, HiveMQ, AWS IoT | Tested against standard MQTT brokers |
| Default Ports | `1883` (Plain TCP) / `8883` (MQTTS with TLS) | Configurable via `MQTT_BROKER_PORT` |

---

## Topic Architecture & Contract

Topics follow an explicit taxonomy:

| Direction | Topic Pattern | QoS | Purpose |
|---|---|---|---|
| Outbound | `liveengine/v1/device/{device_id}/command` | 1 (P0/P1) / 0 (P2-P4) | Command dispatched to target device |
| Inbound | `liveengine/v1/device/{device_id}/heartbeat` | 0 | Device liveness and uptime ping |
| Inbound | `liveengine/v1/device/{device_id}/telemetry` | 0 | Sensor metrics and command execution ack/reject |
| Outbound | `liveengine/v1/system/status` | 0 | Engine lifecycle broadcasts |

> [!IMPORTANT]
> User input (TikTok username, comments, gift names) is **never** interpolated into MQTT topic paths. Topic structure is immutable and scoped strictly by verified `device_id`.

---

## Message Contract

### Outbound Command Payload (`liveengine/v1/device/{device_id}/command`)
```json
{
  "schema_version": "1.0",
  "message_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
  "event_id": "evt_live_12345",
  "action_id": "act_spawn_67890",
  "device_id": "esp32_led_01",
  "command": "PLAY_EFFECT",
  "timestamp": "2026-09-21T22:15:00.000000Z",
  "expires_at": "2026-09-21T22:15:30.000000Z",
  "payload": {
    "action_type": "PLAY_EFFECT",
    "target_device_id": "esp32_led_01",
    "effect": "rainbow_burst",
    "duration_ms": 1500
  }
}
```

### Inbound Heartbeat Payload (`liveengine/v1/device/{device_id}/heartbeat`)
```json
{
  "status": "OK",
  "uptime_s": 3600,
  "free_heap_bytes": 184320
}
```

### Inbound Telemetry Payload (`liveengine/v1/device/{device_id}/telemetry`)
```json
{
  "device_id": "esp32_led_01",
  "status": "ACK",
  "last_command_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
  "temperature_c": 38.5
}
```

---

## QoS & Retention Policy

- **QoS 1 (At Least Once):** Applied exclusively to critical and high-priority interactions (`Priority.P0`, `Priority.P1`).
- **QoS 0 (At Most Once):** Applied to non-critical, ambient, or high-frequency telemetry/effects (`Priority.P2`, `Priority.P3`, `Priority.P4`).
- **Retained Messages:** **Forbidden** (`retain=False`) for one-shot actuation commands. A newly connected ESP32 must not execute stale physical actions upon booting.

---

## Configuration Reference

| Environment Variable | Default | Description |
|---|---|---|
| `MQTT_ENABLED` | `false` | Enables/disables the MQTT adapter |
| `MQTT_BROKER_HOST` | `localhost` | MQTT broker hostname or IP |
| `MQTT_BROKER_PORT` | `1883` | MQTT port (1883 TCP, 8883 TLS) |
| `MQTT_CLIENT_ID` | `liveengine-{uuid}` | Client identification string |
| `MQTT_USERNAME` | `""` | Optional broker username |
| `MQTT_PASSWORD` | `""` | Optional broker password |
| `MQTT_KEEPALIVE` | `60` | Keepalive interval in seconds |
| `MQTT_TLS_ENABLED` | `false` | Enable TLS encryption |
| `MQTT_RECONNECT_ENABLED` | `true` | Auto-reconnect on broker disconnection |
| `MQTT_RECONNECT_DELAYS` | `2,4,8,16,30` | Exponential backoff delay steps |
| `MQTT_MAX_QUEUE_SIZE` | `500` | Global maximum pending queue items |
| `MQTT_MAX_PAYLOAD_BYTES` | `4096` | Payload size upper bound (rejects oversized) |
| `MQTT_PUBLISH_RATE_LIMIT` | `10.0` | Maximum messages published per second |
| `MQTT_COMMAND_TTL` | `30.0` | Command expiration window in seconds |
| `MQTT_ALLOWED_DEVICES` | `esp32_led_01,...`| Comma-separated list of approved device IDs |
| `MQTT_ALLOWED_COMMANDS`| `SPAWN_AVATAR,...`| Comma-separated list of permitted actions |

---

## Observability & Metrics

The adapter collects and exposes real-time telemetry via `adapter.metrics.snapshot()`:

- `mqtt_connection_state`: Current lifecycle state (`DISCONNECTED`, `CONNECTING`, `CONNECTED`, `RECONNECTING`, `STOPPING`, `STOPPED`, `FAILED`).
- `mqtt_connect_attempts_total`: Total broker connection attempts.
- `mqtt_reconnect_total`: Reconnection count.
- `mqtt_publish_total`: Messages successfully dispatched to broker.
- `mqtt_publish_failures_total`: Broker publication errors.
- `mqtt_messages_dropped_total`: Drops due to backpressure / bounded queue saturation.
- `mqtt_messages_coalesced_total`: Duplicate events filtered by idempotency cache.
- `mqtt_messages_expired_total`: Messages discarded due to TTL expiry prior to transmission.
- `mqtt_payload_rejected_total`: Invalids due to size or unauthorized device/command.
- `device_heartbeat_total`: Total heartbeats received from physical nodes.
- `device_command_rejected_total`: Commands rejected locally by ESP32 devices.

---

## Testing & Verification

1. **Unit & Contract Testing:** `src/tests/unit/test_mqtt_adapter.py`
   - Validates serialization, QoS assignment, queue priority eviction, deduplication, oversized payload rejection, rate limiting, and telemetry loop parsing.
2. **Flood & Stress Testing:**
   - Evaluates queue bounds under 1,000 mixed-priority event bursts without broker lockups or memory leaks.
3. **Integration Testing:**
   - Mocked broker loops verify reconnect backoff, telemetry handling, and graceful shutdown.
