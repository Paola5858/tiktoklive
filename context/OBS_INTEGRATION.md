# Phase 8 — OBS Integration & Stream Automation

## Architecture Overview

The OBS Integration module operates as a **decoupled, non-blocking consumer** (`OBSAdapter`) registered with the Event Engine Dispatcher.

```
TikTok Event → Normalizer → Event Engine → Priority Queue
   → Dispatcher.dispatch(event)
       ├── InteractionConsumer → InteractionRuleEngine → RobloxBridge
       └── OBSAdapter.handle(event)
               ↓ (non-blocking: enqueues action and returns immediately)
           OBSPriorityQueue (bounded, priority-ordered, deduplicated, expiring)
               ↓ (isolated background worker task)
           OBSAdapter._worker_task
               ↓ (asyncio executor wrapper around obsws-python ReqClient)
           OBS Studio (obs-websocket 5.x, port 4455)
```

### Key Architectural Guarantees
1. **Zero-Latency Ingestion Impact:** `handle()` only enqueues an `OBSAction` and immediately returns. I/O with OBS happens asynchronously in a separate background worker task.
2. **Failure Isolation:** Errors or timeouts communicating with OBS Studio never propagate back to the TikTok ingestion pipeline, the Priority Queue, or the Roblox Bridge.
3. **Graceful Degradation:** If OBS is disabled (`OBS_ENABLED=false`), `can_handle()` returns `False` and zero resources are consumed. If OBS goes offline, the adapter queues actions up to capacity and retries with backoff without crashing the application.

---

## Protocol & Specification

| Property | Value | Notes |
|---|---|---|
| Protocol Version | **obs-websocket 5.x** | `rpcVersion = 1` |
| Default Port | **4455** | Configurable via `OBS_PORT` |
| Client Library | `obsws-python ≥ 1.7` | PyPI package: `obsws-python` |
| Transport | WebSocket | JSON RPC 2.0-like protocol |
| Authentication | SHA-256 + Base64 | `base64(SHA256(base64(SHA256(password+salt)) + challenge))` |
| OBS Version | OBS Studio ≥ 28.0 | Native websocket support built-in |

> [!IMPORTANT]
> `SetSceneItemEnabled` requires a `sceneItemId` (integer), **not** a source name. The adapter resolves `source_name` → `sceneItemId` using `GetSceneItemId` prior to updating scene item visibility.

---

## Credentials & Security Contract

> [!WARNING]
> Credentials (`OBS_PASSWORD`) MUST only be loaded from environment variables or a `.env` file. They are NEVER hardcoded, logged, or exposed in `/health` API endpoints.

- **Password Masking:** `_sanitize_error()` masks passwords, tokens, and authorization parameters in error tracebacks before logging.
- **Strict Allowlists:** `allowed_scenes` and `allowed_sources` enforce strict boundaries. Unlisted scene names or source names are rejected at validation time.
- **External Path Protection:** `OBS_TRIGGER_MEDIA` rejects arbitrary file paths or external URLs in incoming event payloads. Media sources must be configured directly inside OBS Studio.
- **Text Input Sanitization:** Control characters are stripped from user text before updating OBS text elements (e.g. text ticker overlays), preventing display corruption or injection.

---

## Supported Actions

The OBS adapter supports four strictly typed actions:

1. **`OBS_SET_SCENE`**
   - Changes the current program scene.
   - Parameters: `{"scene_name": "<name>"}`
   - Idempotent: If OBS is already on the target scene, the request is skipped.
   - Throttled: Enforces `scene_change_cooldown_s` to prevent scene flickering.

2. **`OBS_SET_SOURCE_ENABLED`**
   - Shows or hides a source inside a scene.
   - Parameters: `{"scene_name": "<scene>", "source_name": "<source>", "enabled": true|false}`

3. **`OBS_TRIGGER_MEDIA`**
   - Restarts/replays a configured media input source (e.g., sound effect or video overlay).
   - Parameters: `{"source_name": "<source>"}`

4. **`OBS_SET_INPUT_TEXT`**
   - Updates the text content of a Text (GDI+/FreeType) input source for live tickers or overlays.
   - Parameters: `{"source_name": "<source>", "text": "<content>", "input_key": "text"}`

---

## Configuration Reference

The configuration is managed by `OBSConfig` (loaded via `OBSConfig.from_env()`):

| Environment Variable | Default | Description |
|---|---|---|
| `OBS_ENABLED` | `false` | Set to `true` to enable OBS integration |
| `OBS_HOST` | `localhost` | Hostname or IP of OBS Studio instance |
| `OBS_PORT` | `4455` | Port of obs-websocket server |
| `OBS_PASSWORD` | `""` | Password set in OBS WebSocket settings |
| `OBS_CONNECT_TIMEOUT_S` | `10.0` | Initial connection timeout in seconds |
| `OBS_REQUEST_TIMEOUT_S` | `5.0` | Request timeout per websocket call |
| `OBS_RECONNECT_DELAYS` | `2,4,8,16,30` | Backoff sequence for reconnections (seconds) |
| `OBS_SCENE_COOLDOWN_S` | `3.0` | Cooldown period between scene changes |
| `OBS_ALLOWED_SCENES` | `""` | Comma-separated list of allowed scene names |
| `OBS_ALLOWED_SOURCES` | `""` | Comma-separated list of allowed source names |
| `OBS_QUEUE_MAXSIZE` | `100` | Maximum queue depth per priority level |

### Action Rules (`configs/obs_actions.json`)

Event-to-action mappings are configured via `configs/obs_actions.json`:
```json
{
  "schema_version": "1.0",
  "allowed_scenes": ["Main", "BRB", "Starting", "Ending"],
  "allowed_sources": ["alert_overlay", "event_ticker", "webcam"],
  "rules": [
    {
      "match_event_type": "GIFT",
      "match_priority": ["P1"],
      "action": "OBS_SET_SCENE",
      "params": {"scene_name": "Main"},
      "priority": 1,
      "expires_after_s": 30,
      "cooldown_s": 5.0
    }
  ]
}
```

---

## Resilience & Manual Override Handling

### Priority Queue Bounded Overflow
The adapter maintains an `OBSPriorityQueue` with 5 priority levels (P0 through P4). When the queue reaches capacity:
- Low-priority items (P3/P4) are dropped.
- High-priority items (P0/P1) drop older items at the same priority level if full, preserving high-value stream alerts.

### Temporary Scene Effects
When executing a temporary scene switch (e.g., switch to celebration scene for 5 seconds and switch back):
- The adapter captures `initial_scene`.
- After sleeping for the duration, it checks the current OBS program scene.
- If the streamer manually switched scenes during the effect, the automatic restore is **cancelled** to respect manual streamer control.

---

## Verification & Health Check

### Health Endpoint Snapshot (`/health`)
The adapter reports its health status via `health_snapshot()`:
```json
{
  "status": "healthy",
  "enabled": true,
  "connected": true,
  "host": "localhost",
  "port": 4455,
  "last_success_at": "2026-09-21T20:10:00Z",
  "last_error": null,
  "last_request_latency_ms": 4.2,
  "queue_depth": 0,
  "current_scene": "Main",
  "metrics": {
    "obs_connect_attempts_total": 1,
    "obs_reconnects_total": 0,
    "obs_requests_total": 42,
    "obs_request_failures_total": 0,
    "obs_scene_changes": 3,
    "obs_source_updates": 12,
    "obs_media_triggers": 5,
    "obs_actions_queued": 42,
    "obs_actions_dropped": 0,
    "obs_actions_expired": 0
  }
}
```
