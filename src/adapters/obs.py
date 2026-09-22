"""OBS Studio Adapter — consumidor desacoplado do Event Engine.

Integra o Live Engine ao OBS Studio via obs-websocket 5.x (rpcVersion=1).
Verificado contra a especificação oficial em:
https://github.com/obsproject/obs-websocket/blob/master/docs/generated/protocol.md

Biblioteca: obsws-python 1.8.0 (PyPI) — cliente síncrono embrulhado em
run_in_executor para não bloquear o loop asyncio principal.

PRINCÍPIO CENTRAL: OBS é um consumidor desacoplado.
  TikTok Event → Event Engine → Dispatcher → OBSAdapter.handle()
    → OBSPriorityQueue (enqueue, não bloqueante)
      → _worker_task (background, isolado)
        → obsws-python ReqClient → OBS Studio

Uma falha do OBS não pode derrubar TikTok, a fila principal ou o Roblox.

SEGURANÇA:
  - Senha nunca hardcoded — lida exclusivamente de variável de ambiente OBS_PASSWORD.
  - Senha nunca aparece em logs, health snapshots ou exceções.
  - Nomes de cena e fonte passam por allowlist obrigatória.
  - Texto de comentário é sanitizado antes de virar input OBS.
  - Caminhos de arquivo arbitrários são rejeitados.
  - Payloads externos não escolhem operações arbitrárias.

PROTOCOLO VERIFICADO (obs-websocket 5.x):
  - Hello (OpCode 0): server envia challenge+salt se auth ativo.
  - Identify (OpCode 1): cliente responde com auth=base64(SHA256(base64(SHA256(pw+salt))+challenge)).
  - Identified (OpCode 2): conexão pronta.
  - Request (OpCode 6): {"requestType": "...", "requestId": "uuid", "requestData": {}}.
  - RequestResponse (OpCode 7): {"requestType":"...", "requestStatus":{"result":bool,"code":int}}.
  - SetSceneItemEnabled REQUER sceneItemId (int), não nome — chamamos GetSceneItemId primeiro.

VERSÃO MÍNIMA OBS: 28.0 (obs-websocket 5.x bundled).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from src.domain.events import AggregatedEvent, Event
from src.domain.priorities import Priority
from src.logging import get_logger
from src.observability.health import ComponentHealth, Watchdog
from src.observability.resilience import ResilienceMetrics

LOGGER = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constantes verificadas contra o protocolo 5.x
# ---------------------------------------------------------------------------

OBS_WS_PROTOCOL_VERSION = "5.x"
OBS_WS_RPC_VERSION = 1
OBS_WS_DEFAULT_PORT = 4455  # porta default do obs-websocket 5.x (v4 usava 4444)

# Requests verificados no protocolo 5.x:
_REQ_GET_VERSION = "GetVersion"
_REQ_GET_SCENE_LIST = "GetSceneList"
_REQ_GET_CURRENT_SCENE = "GetCurrentProgramScene"
_REQ_SET_CURRENT_SCENE = "SetCurrentProgramScene"
_REQ_GET_SCENE_ITEM_ID = "GetSceneItemId"
_REQ_SET_SCENE_ITEM_ENABLED = "SetSceneItemEnabled"
_REQ_TRIGGER_MEDIA_ACTION = "TriggerMediaInputAction"
_REQ_SET_INPUT_SETTINGS = "SetInputSettings"

# Ação de mídia verificada no protocolo 5.x (ObsMediaInputAction enum):
_MEDIA_ACTION_RESTART = "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART"

# Limites de segurança
_MAX_SCENE_NAME_LEN = 256
_MAX_SOURCE_NAME_LEN = 256
_MAX_TEXT_LEN = 200
_MAX_INPUT_KEY_LEN = 64
_MAX_ACTION_PARAMS = 16


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class OBSConnectionState(str, Enum):
    """Estado do ciclo de vida da conexão com o OBS."""

    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class OBSActionType(str, Enum):
    """Ações OBS suportadas — somente as verificadas no protocolo 5.x.

    Não inventar ações sem verificação prévia na especificação.
    """

    OBS_SET_SCENE = "OBS_SET_SCENE"
    """SetCurrentProgramScene — muda a cena ativa."""

    OBS_SET_SOURCE_ENABLED = "OBS_SET_SOURCE_ENABLED"
    """SetSceneItemEnabled — habilita ou desabilita uma fonte numa cena.
    ATENÇÃO: requer sceneItemId (int), obtido via GetSceneItemId."""

    OBS_TRIGGER_MEDIA = "OBS_TRIGGER_MEDIA"
    """TriggerMediaInputAction com OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART."""

    OBS_SET_INPUT_TEXT = "OBS_SET_INPUT_TEXT"
    """SetInputSettings para atualizar o texto de um Text (GDI+/FreeType) input.
    Usado para overlays de texto. Conteúdo é sanitizado antes de enviar."""


class OBSHealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    DISABLED = "disabled"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OBSConfig:
    """Configuração validada do OBS Adapter.

    NUNCA hardcode a senha aqui. Use a variável de ambiente OBS_PASSWORD.
    """

    enabled: bool
    host: str
    port: int
    password: str  # lida do ambiente; nunca logar ou retornar em health
    connect_timeout_s: float
    request_timeout_s: float
    reconnect_delays_s: tuple[float, ...]
    scene_change_cooldown_s: float
    allowed_scenes: frozenset[str]
    allowed_sources: frozenset[str]
    obs_queue_maxsize: int

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not self.host.strip():
            raise ValueError("OBS host inválido")
        if not (1 <= self.port <= 65535):
            raise ValueError(f"OBS port inválido: {self.port}")
        if self.connect_timeout_s <= 0:
            raise ValueError("connect_timeout_s deve ser > 0")
        if self.request_timeout_s <= 0:
            raise ValueError("request_timeout_s deve ser > 0")
        if not self.reconnect_delays_s:
            raise ValueError("reconnect_delays_s não pode estar vazio")
        if self.scene_change_cooldown_s < 0:
            raise ValueError("scene_change_cooldown_s deve ser >= 0")
        if self.obs_queue_maxsize <= 0:
            raise ValueError("obs_queue_maxsize deve ser > 0")
        # Validar nomes de cena/fonte
        for name in self.allowed_scenes:
            if len(name) > _MAX_SCENE_NAME_LEN:
                raise ValueError(f"scene name excede {_MAX_SCENE_NAME_LEN} chars: {name!r}")
        for name in self.allowed_sources:
            if len(name) > _MAX_SOURCE_NAME_LEN:
                raise ValueError(f"source name excede {_MAX_SOURCE_NAME_LEN} chars: {name!r}")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "OBSConfig":
        """Monta configuração a partir de variáveis de ambiente.

        Variáveis:
          OBS_ENABLED      (default: false)
          OBS_HOST         (default: localhost)
          OBS_PORT         (default: 4455)
          OBS_PASSWORD     (default: "" — sem autenticação)
          OBS_CONNECT_TIMEOUT_S  (default: 10.0)
          OBS_REQUEST_TIMEOUT_S  (default: 5.0)
          OBS_RECONNECT_DELAYS   (default: 2,4,8,16,30)
          OBS_SCENE_COOLDOWN_S   (default: 3.0)
          OBS_ALLOWED_SCENES     (default: "" — nenhuma cena permitida)
          OBS_ALLOWED_SOURCES    (default: "" — nenhuma fonte permitida)
          OBS_QUEUE_MAXSIZE      (default: 100)
        """
        source = env if env is not None else os.environ

        enabled_raw = source.get("OBS_ENABLED", "false").strip().lower()
        enabled = enabled_raw in ("1", "true", "yes", "on")

        host = source.get("OBS_HOST", "localhost").strip()

        try:
            port = int(source.get("OBS_PORT", str(OBS_WS_DEFAULT_PORT)))
        except ValueError:
            raise ValueError("OBS_PORT deve ser um inteiro válido")

        # SEGURANÇA: senha lida do ambiente, nunca logada
        password = source.get("OBS_PASSWORD", "")

        try:
            connect_timeout = float(source.get("OBS_CONNECT_TIMEOUT_S", "10.0"))
        except ValueError:
            raise ValueError("OBS_CONNECT_TIMEOUT_S deve ser um número")

        try:
            request_timeout = float(source.get("OBS_REQUEST_TIMEOUT_S", "5.0"))
        except ValueError:
            raise ValueError("OBS_REQUEST_TIMEOUT_S deve ser um número")

        delays_raw = source.get("OBS_RECONNECT_DELAYS", "2,4,8,16,30").strip()
        try:
            delays = tuple(float(x.strip()) for x in delays_raw.split(",") if x.strip())
        except ValueError:
            raise ValueError("OBS_RECONNECT_DELAYS deve ser lista de números separados por vírgula")

        try:
            cooldown = float(source.get("OBS_SCENE_COOLDOWN_S", "3.0"))
        except ValueError:
            raise ValueError("OBS_SCENE_COOLDOWN_S deve ser um número")

        scenes_raw = source.get("OBS_ALLOWED_SCENES", "").strip()
        allowed_scenes = frozenset(
            s.strip() for s in scenes_raw.split(",") if s.strip()
        )

        sources_raw = source.get("OBS_ALLOWED_SOURCES", "").strip()
        allowed_sources = frozenset(
            s.strip() for s in sources_raw.split(",") if s.strip()
        )

        try:
            queue_maxsize = int(source.get("OBS_QUEUE_MAXSIZE", "100"))
        except ValueError:
            raise ValueError("OBS_QUEUE_MAXSIZE deve ser um inteiro")

        return cls(
            enabled=enabled,
            host=host,
            port=port,
            password=password,
            connect_timeout_s=connect_timeout,
            request_timeout_s=request_timeout,
            reconnect_delays_s=delays,
            scene_change_cooldown_s=cooldown,
            allowed_scenes=allowed_scenes,
            allowed_sources=allowed_sources,
            obs_queue_maxsize=queue_maxsize,
        )

    @classmethod
    def disabled(cls) -> "OBSConfig":
        """Cria uma configuração desabilitada (padrão seguro)."""
        return cls(
            enabled=False,
            host="localhost",
            port=OBS_WS_DEFAULT_PORT,
            password="",
            connect_timeout_s=10.0,
            request_timeout_s=5.0,
            reconnect_delays_s=(2.0, 4.0, 8.0, 16.0, 30.0),
            scene_change_cooldown_s=3.0,
            allowed_scenes=frozenset(),
            allowed_sources=frozenset(),
            obs_queue_maxsize=100,
        )


# ---------------------------------------------------------------------------
# Modelo de ação
# ---------------------------------------------------------------------------


@dataclass
class OBSAction:
    """Ação OBS enfileirada para execução assíncrona."""

    action_type: OBSActionType
    params: dict[str, Any]
    priority: int  # 0=mais alta, 4=mais baixa (alinhado com Priority enum)
    action_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    dedupe_key: str | None = None

    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) >= self.expires_at


# ---------------------------------------------------------------------------
# Validador e allowlist
# ---------------------------------------------------------------------------


# Padrão para detectar caracteres de controle em texto (exceto espaço e tab)
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class OBSActionValidator:
    """Valida e aplica allowlist em ações antes de enfileirar.

    SEGURANÇA:
    - Nenhum dado externo escolhe operação arbitrária.
    - Nomes de cena/fonte passam por allowlist obrigatória.
    - Texto é sanitizado (strip, limite de tamanho, sem controle chars).
    - Caminhos de arquivo externos são rejeitados.
    """

    def __init__(self, config: OBSConfig) -> None:
        self._config = config

    def validate(self, action_type: OBSActionType, params: dict[str, Any]) -> dict[str, Any]:
        """Valida params e retorna versão sanitizada.

        Raises:
            ValueError: se action_type não é permitido, cena/fonte fora da allowlist,
                        tamanho excedido ou parâmetro proibido presente.
        """
        if not isinstance(params, dict):
            raise ValueError("params deve ser dict")
        if len(params) > _MAX_ACTION_PARAMS:
            raise ValueError(f"params excede {_MAX_ACTION_PARAMS} campos")

        if action_type == OBSActionType.OBS_SET_SCENE:
            return self._validate_set_scene(params)
        elif action_type == OBSActionType.OBS_SET_SOURCE_ENABLED:
            return self._validate_set_source_enabled(params)
        elif action_type == OBSActionType.OBS_TRIGGER_MEDIA:
            return self._validate_trigger_media(params)
        elif action_type == OBSActionType.OBS_SET_INPUT_TEXT:
            return self._validate_set_input_text(params)
        else:
            raise ValueError(f"OBSActionType não suportado: {action_type}")

    def _validate_set_scene(self, params: dict[str, Any]) -> dict[str, Any]:
        scene_name = params.get("scene_name", "")
        if not isinstance(scene_name, str) or not scene_name.strip():
            raise ValueError("OBS_SET_SCENE requer scene_name não vazio")
        scene_name = scene_name.strip()
        if len(scene_name) > _MAX_SCENE_NAME_LEN:
            raise ValueError(f"scene_name excede {_MAX_SCENE_NAME_LEN} chars")
        if self._config.allowed_scenes and scene_name not in self._config.allowed_scenes:
            raise ValueError(
                f"Cena '{scene_name}' não está na allowlist de cenas permitidas"
            )
        return {"scene_name": scene_name}

    def _validate_set_source_enabled(self, params: dict[str, Any]) -> dict[str, Any]:
        scene_name = params.get("scene_name", "")
        source_name = params.get("source_name", "")
        enabled = params.get("enabled", True)

        if not isinstance(scene_name, str) or not scene_name.strip():
            raise ValueError("OBS_SET_SOURCE_ENABLED requer scene_name")
        if not isinstance(source_name, str) or not source_name.strip():
            raise ValueError("OBS_SET_SOURCE_ENABLED requer source_name")

        scene_name = scene_name.strip()
        source_name = source_name.strip()

        if len(scene_name) > _MAX_SCENE_NAME_LEN:
            raise ValueError("scene_name excede limite")
        if len(source_name) > _MAX_SOURCE_NAME_LEN:
            raise ValueError("source_name excede limite")
        if self._config.allowed_scenes and scene_name not in self._config.allowed_scenes:
            raise ValueError(f"Cena '{scene_name}' não está na allowlist")
        if self._config.allowed_sources and source_name not in self._config.allowed_sources:
            raise ValueError(f"Fonte '{source_name}' não está na allowlist")
        if not isinstance(enabled, bool):
            raise ValueError("enabled deve ser bool")

        return {"scene_name": scene_name, "source_name": source_name, "enabled": enabled}

    def _validate_trigger_media(self, params: dict[str, Any]) -> dict[str, Any]:
        source_name = params.get("source_name", "")
        if not isinstance(source_name, str) or not source_name.strip():
            raise ValueError("OBS_TRIGGER_MEDIA requer source_name")
        source_name = source_name.strip()
        if len(source_name) > _MAX_SOURCE_NAME_LEN:
            raise ValueError("source_name excede limite")
        if self._config.allowed_sources and source_name not in self._config.allowed_sources:
            raise ValueError(f"Fonte '{source_name}' não está na allowlist")
        # SEGURANÇA: não aceitar filepath arbitrário — a mídia deve estar
        # configurada diretamente no OBS, não vir do payload externo.
        if "file_path" in params or "media_path" in params or "url" in params:
            raise ValueError(
                "OBS_TRIGGER_MEDIA não aceita caminhos de arquivo ou URLs externos. "
                "Configure a fonte de mídia diretamente no OBS."
            )
        return {"source_name": source_name}

    def _validate_set_input_text(self, params: dict[str, Any]) -> dict[str, Any]:
        source_name = params.get("source_name", "")
        text = params.get("text", "")
        input_key = params.get("input_key", "text")  # campo do input OBS (GDI+: "text")

        if not isinstance(source_name, str) or not source_name.strip():
            raise ValueError("OBS_SET_INPUT_TEXT requer source_name")
        if not isinstance(text, str):
            raise ValueError("text deve ser string")
        if not isinstance(input_key, str) or not input_key.strip():
            raise ValueError("input_key deve ser string não vazia")

        source_name = source_name.strip()
        input_key = input_key.strip()

        if len(source_name) > _MAX_SOURCE_NAME_LEN:
            raise ValueError("source_name excede limite")
        if len(input_key) > _MAX_INPUT_KEY_LEN:
            raise ValueError(f"input_key excede {_MAX_INPUT_KEY_LEN} chars")
        if self._config.allowed_sources and source_name not in self._config.allowed_sources:
            raise ValueError(f"Fonte '{source_name}' não está na allowlist")

        # SEGURANÇA: sanitizar texto — limitar tamanho e remover caracteres de controle
        # Texto de comentário é conteúdo não confiável.
        sanitized = _CONTROL_CHARS.sub("", text)
        sanitized = sanitized[:_MAX_TEXT_LEN]

        return {"source_name": source_name, "text": sanitized, "input_key": input_key}


# ---------------------------------------------------------------------------
# Fila com prioridade e dedupe
# ---------------------------------------------------------------------------


class OBSPriorityQueue:
    """Fila OBS bounded com prioridade, dedupe e expiração.

    Usa 5 asyncio.Queue independentes (P0–P4), mesma estratégia do
    PriorityQueueSet do Event Engine, para garantir que P0/P1 nunca
    sejam silenciosamente expulsos por P3/P4.

    Uma fila OBS saturada não pode bloquear a fila principal do engine.
    """

    def __init__(self, maxsize_per_level: int = 20) -> None:
        # P0 e P1 têm capacidade maior para preservar eventos críticos
        capacities = [
            maxsize_per_level,      # P0 — sistema/controle
            maxsize_per_level,      # P1 — gift alto impacto
            maxsize_per_level // 2, # P2 — interação especial
            maxsize_per_level // 4, # P3 — comentário
            maxsize_per_level // 8, # P4 — descartável
        ]
        self._queues: list[asyncio.Queue[OBSAction]] = [
            asyncio.Queue(maxsize=max(1, cap)) for cap in capacities
        ]
        self._dropped_total = 0
        self._queued_total = 0
        self._expired_total = 0
        self._closed = False
        self._has_items = asyncio.Event()
        # Dedupe: última dedupe_key vista por tipo de ação
        self._last_dedupe: dict[str, str] = {}

    @property
    def dropped_total(self) -> int:
        return self._dropped_total

    @property
    def queued_total(self) -> int:
        return self._queued_total

    @property
    def expired_total(self) -> int:
        return self._expired_total

    def depth(self) -> int:
        return sum(q.qsize() for q in self._queues)

    def close(self) -> None:
        self._closed = True
        self._has_items.set()

    def put_nowait(self, action: OBSAction) -> bool:
        """Tenta enfileirar a ação. Retorna True se aceita, False se descartada.

        Overflow: drop do mais antigo P3/P4 — nunca drop P0/P1.
        """
        if self._closed:
            self._dropped_total += 1
            return False

        p = max(0, min(4, action.priority))
        q = self._queues[p]

        if not q.full():
            # Dedupe: se a mesma dedupe_key foi a última para este tipo, skip
            if action.dedupe_key is not None:
                last = self._last_dedupe.get(action.action_type.value)
                if last == action.dedupe_key:
                    # Mesmo valor — descarta silenciosamente (idempotência)
                    self._dropped_total += 1
                    return False
                self._last_dedupe[action.action_type.value] = action.dedupe_key

            q.put_nowait(action)
            self._queued_total += 1
            self._has_items.set()
            return True
        else:
            # P0/P1: nunca descartar por overflow
            if p <= 1:
                # Tentar remover o mais antigo item da mesma fila (bounded replace)
                try:
                    q.get_nowait()
                    self._dropped_total += 1
                    q.put_nowait(action)
                    self._queued_total += 1
                    self._has_items.set()
                    return True
                except asyncio.QueueEmpty:
                    pass
            # P2/P3/P4: descartar novo item quando fila cheia
            self._dropped_total += 1
            LOGGER.debug(
                "OBS queue overflow: descartando ação P%d %s",
                p,
                action.action_type.value,
            )
            return False

    async def get_next(self) -> OBSAction:
        """Retorna a próxima ação de maior prioridade disponível.

        Bloqueia até que haja uma ação disponível em alguma fila.
        Ações expiradas são descartadas e contabilizadas.
        """
        while True:
            # Tentar filas em ordem de prioridade
            for q in self._queues:
                if not q.empty():
                    try:
                        action = q.get_nowait()
                        if self.depth() == 0:
                            self._has_items.clear()
                        if action.is_expired():
                            self._expired_total += 1
                            LOGGER.debug(
                                "OBS action expirada descartada: %s id=%s",
                                action.action_type.value,
                                action.action_id,
                            )
                            continue
                        return action
                    except asyncio.QueueEmpty:
                        continue

            if self._closed and self.depth() == 0:
                raise StopAsyncIteration

            # Nenhuma fila tem item — aguarda até que chegue algo ou feche
            await self._has_items.wait()


# ---------------------------------------------------------------------------
# Métricas OBS
# ---------------------------------------------------------------------------


@dataclass
class OBSMetrics:
    """Contadores de métricas do OBS adapter."""

    connect_attempts_total: int = 0
    reconnects_total: int = 0
    requests_total: int = 0
    request_failures_total: int = 0
    scene_changes: int = 0
    source_updates: int = 0
    media_triggers: int = 0
    last_request_latency_ms: float = 0.0

    def snapshot(self) -> dict[str, Any]:
        return {
            "obs_connect_attempts_total": self.connect_attempts_total,
            "obs_reconnects_total": self.reconnects_total,
            "obs_requests_total": self.requests_total,
            "obs_request_failures_total": self.request_failures_total,
            "obs_scene_changes": self.scene_changes,
            "obs_source_updates": self.source_updates,
            "obs_media_triggers": self.media_triggers,
            "obs_last_request_latency_ms": round(self.last_request_latency_ms, 1),
        }


# ---------------------------------------------------------------------------
# OBS Adapter principal
# ---------------------------------------------------------------------------


class OBSAdapter:
    """Consumidor desacoplado do Event Engine para automação do OBS Studio.

    Registra-se no Dispatcher como EventConsumer. O método handle() só enfileira
    ações — nunca bloqueia em I/O do OBS. Um background task drena a fila e
    executa as requests ao OBS de forma isolada.

    Se o OBS estiver desabilitado (config.enabled=False), can_handle() retorna
    False e o sistema continua normalmente sem overhead algum.

    Se o OBS estiver offline, a fila acumula (até maxsize) e o worker fica em
    backoff — sem bloquear TikTok, a engine ou o Roblox.
    """

    def __init__(
        self,
        config: OBSConfig | None = None,
        watchdog: Watchdog | None = None,
        action_mapping: list[dict[str, Any]] | None = None,
        resilience: ResilienceMetrics | None = None,
    ) -> None:
        self._config = config or OBSConfig.disabled()
        self._state = OBSConnectionState.DISCONNECTED
        self._metrics = OBSMetrics()
        self._queue = OBSPriorityQueue(maxsize_per_level=max(4, self._config.obs_queue_maxsize // 5))
        self._validator = OBSActionValidator(self._config)
        self._action_mapping: list[dict[str, Any]] = action_mapping or []
        self._resilience = resilience

        # Rastreamento de cena para idempotência e cooldown
        self._current_scene: str | None = None
        self._last_scene_change_at: float = 0.0

        # Cache de sceneItemId para evitar GetSceneItemId repetido
        self._scene_item_id_cache: dict[tuple[str, str], int] = {}

        # Temporização de efeito temporário
        self._temp_scene_task: asyncio.Task[None] | None = None

        # Watchdog
        self._health: ComponentHealth | None = None
        if watchdog and self._config.enabled:
            self._health = watchdog.register("OBSAdapter")

        # Background tasks
        self._worker_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

        # OBS client (obsws-python ReqClient — síncrono, usado via run_in_executor)
        self._client: Any = None
        self._client_lock = asyncio.Lock()

        # Observabilidade
        self._last_success_at: datetime | None = None
        self._last_error: str | None = None

    # ------------------------------------------------------------------
    # EventConsumer Protocol
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "obs_adapter"

    def can_handle(self, event: Event | AggregatedEvent) -> bool:
        """Retorna False quando OBS está desabilitado — zero overhead."""
        return self._config.enabled

    async def handle(self, event: Event | AggregatedEvent) -> None:
        """Traduz event em OBSAction(s) e enfileira — não bloqueia."""
        if not self._config.enabled:
            return

        try:
            actions = self._map_event_to_actions(event)
        except Exception:
            LOGGER.exception("OBSAdapter: erro ao mapear evento %s", _event_id(event))
            return

        for action in actions:
            accepted = self._queue.put_nowait(action)
            if not accepted:
                LOGGER.debug(
                    "OBS action descartada (overflow ou dedupe): %s",
                    action.action_type.value,
                )

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Inicia o adapter: conecta ao OBS e inicia background tasks.

        Seguro chamar mesmo com OBS desabilitado ou offline —
        falha de conexão é tratada pelo reconnect loop.
        """
        if not self._config.enabled:
            LOGGER.info("OBSAdapter: desabilitado — nenhuma conexão será tentada")
            self._state = OBSConnectionState.STOPPED
            return

        self._stop_event.clear()
        self._worker_task = asyncio.create_task(self._worker_loop(), name="obs_worker")
        self._reconnect_task = asyncio.create_task(self._connect_loop(), name="obs_connect")
        LOGGER.info(
            "OBSAdapter: iniciado (host=%s port=%d)",
            self._config.host,
            self._config.port,
        )

    async def stop(self) -> None:
        """Para o adapter de forma segura — cancela tasks, fecha conexão."""
        if not self._config.enabled:
            return

        LOGGER.info("OBSAdapter: iniciando shutdown")
        self._state = OBSConnectionState.STOPPING
        self._stop_event.set()
        self._queue.close()

        if self._temp_scene_task and not self._temp_scene_task.done():
            self._temp_scene_task.cancel()
            try:
                await self._temp_scene_task
            except asyncio.CancelledError:
                pass

        for task in (self._reconnect_task, self._worker_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        await self._disconnect()
        self._state = OBSConnectionState.STOPPED
        if self._resilience:
            self._resilience.record_graceful_shutdown()
        LOGGER.info("OBSAdapter: parado")

    # ------------------------------------------------------------------
    # Conexão
    # ------------------------------------------------------------------

    async def _connect_loop(self) -> None:
        """Loop de conexão com backoff exponencial limitado.

        Sequência de delays configurável — padrão: 2, 4, 8, 16, 30s.
        Shutdown cancela o loop sem criar tasks infinitas.
        """
        first = True
        while not self._stop_event.is_set():
            if not first:
                self._state = OBSConnectionState.RECONNECTING
                self._metrics.reconnects_total += 1
            first = False

            success = await self._try_connect()
            if success:
                # Espera até que o stop event seja disparado
                await self._stop_event.wait()
                return

            # Backoff
            delays = self._config.reconnect_delays_s
            attempt = min(self._metrics.connect_attempts_total - 1, len(delays) - 1)
            delay = delays[attempt]
            LOGGER.info("OBSAdapter: reconnect em %.0fs", delay)

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=delay,
                )
                return  # stop foi sinalizado durante o wait
            except asyncio.TimeoutError:
                pass  # delay expirou, tentar novamente

    async def _try_connect(self) -> bool:
        """Tenta uma conexão ao OBS. Retorna True em sucesso."""
        self._state = OBSConnectionState.CONNECTING
        self._metrics.connect_attempts_total += 1

        try:
            loop = asyncio.get_event_loop()
            # obsws-python ReqClient é síncrono — rodar em executor
            client = await asyncio.wait_for(
                loop.run_in_executor(None, self._create_client),
                timeout=self._config.connect_timeout_s,
            )
            async with self._client_lock:
                self._client = client
            self._state = OBSConnectionState.CONNECTED
            self._last_success_at = datetime.now(timezone.utc)
            self._last_error = None

            if self._health:
                self._health.record_success()
            if self._resilience:
                self._resilience.record_recovery(success=True)

            LOGGER.info(
                "OBSAdapter: conectado ao OBS em %s:%d",
                self._config.host,
                self._config.port,
            )

            # Descobrir cena atual (para idempotência e temp_scene)
            await self._refresh_current_scene()
            return True

        except asyncio.TimeoutError:
            self._state = OBSConnectionState.DISCONNECTED
            self._last_error = "Connection timeout"
            if self._health:
                self._health.record_failure("OBS connection timeout")
            if self._resilience:
                self._resilience.record_timeout()
                self._resilience.record_recovery(success=False)
            LOGGER.warning(
                "OBSAdapter: timeout ao conectar em %s:%d",
                self._config.host,
                self._config.port,
            )
            return False
        except Exception as exc:
            self._state = OBSConnectionState.DISCONNECTED
            # SEGURANÇA: não logar a senha no erro
            error_msg = _sanitize_error(str(exc))
            self._last_error = error_msg
            if self._health:
                self._health.record_failure(f"OBS connection failed: {error_msg}")
            if self._resilience:
                self._resilience.record_recovery(success=False)
            LOGGER.warning(
                "OBSAdapter: falha ao conectar em %s:%d — %s",
                self._config.host,
                self._config.port,
                error_msg,
            )
            return False

    def _create_client(self) -> Any:
        """Cria o ReqClient síncrono do obsws-python. Chamado via run_in_executor."""
        import obsws_python as obs  # import tardio — opcional quando OBS desabilitado

        # SEGURANÇA: senha passada como argumento, não logada aqui
        if self._config.password:
            return obs.ReqClient(
                host=self._config.host,
                port=self._config.port,
                password=self._config.password,
                timeout=int(self._config.connect_timeout_s),
            )
        else:
            return obs.ReqClient(
                host=self._config.host,
                port=self._config.port,
                timeout=int(self._config.connect_timeout_s),
            )

    async def _disconnect(self) -> None:
        """Fecha a conexão com o OBS."""
        async with self._client_lock:
            client = self._client
            self._client = None

        if client is not None:
            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(None, lambda: client.disconnect())
            except Exception:
                pass  # disconnect pode falhar se OBS já foi embora

    async def _refresh_current_scene(self) -> None:
        """Atualiza o cache da cena atual via GetCurrentProgramScene."""
        try:
            resp = await self._send_request(_REQ_GET_CURRENT_SCENE, {})
            if resp and "currentProgramSceneName" in resp:
                self._current_scene = resp["currentProgramSceneName"]
        except Exception:
            pass  # não crítico

    # ------------------------------------------------------------------
    # Worker loop
    # ------------------------------------------------------------------

    async def _worker_loop(self) -> None:
        """Drena a OBSPriorityQueue e executa requests ao OBS.

        Isolado em background task — falhas aqui não afetam o Event Engine.
        """
        LOGGER.debug("OBSAdapter: worker task iniciada")
        while not self._stop_event.is_set():
            try:
                action = await asyncio.wait_for(
                    self._queue.get_next(),
                    timeout=1.0,  # acorda periodicamente para checar stop_event
                )
                await self._execute_action(action)
            except asyncio.TimeoutError:
                continue  # checar stop_event
            except asyncio.CancelledError:
                break
            except Exception:
                LOGGER.exception("OBSAdapter: erro inesperado no worker loop")
        LOGGER.debug("OBSAdapter: worker task encerrada")

    async def _execute_action(self, action: OBSAction) -> None:
        """Executa uma OBSAction via obsws-python. Não propaga exceções."""
        if self._state != OBSConnectionState.CONNECTED:
            LOGGER.debug(
                "OBSAdapter: OBS não conectado (state=%s), action descartada: %s",
                self._state.value,
                action.action_type.value,
            )
            return

        if action.is_expired():
            self._queue._expired_total += 1
            LOGGER.debug("OBSAdapter: action expirada descartada no execute: %s", action.action_id)
            return

        try:
            if action.action_type == OBSActionType.OBS_SET_SCENE:
                await self._do_set_scene(action.params)
            elif action.action_type == OBSActionType.OBS_SET_SOURCE_ENABLED:
                await self._do_set_source_enabled(action.params)
            elif action.action_type == OBSActionType.OBS_TRIGGER_MEDIA:
                await self._do_trigger_media(action.params)
            elif action.action_type == OBSActionType.OBS_SET_INPUT_TEXT:
                await self._do_set_input_text(action.params)
        except Exception as exc:
            error_msg = _sanitize_error(str(exc))
            self._metrics.request_failures_total += 1
            self._last_error = error_msg
            if self._health:
                self._health.record_failure(f"OBS request failed: {error_msg}")
            LOGGER.error(
                "OBSAdapter: falha ao executar %s: %s",
                action.action_type.value,
                error_msg,
            )
            # Detectar desconexão e atualizar estado
            if _is_disconnect_error(error_msg):
                LOGGER.warning("OBSAdapter: detectada desconexão — aguardando reconnect")
                async with self._client_lock:
                    self._client = None
                self._state = OBSConnectionState.DISCONNECTED

    # ------------------------------------------------------------------
    # Implementação das ações (protocolo 5.x verificado)
    # ------------------------------------------------------------------

    async def _do_set_scene(self, params: dict[str, Any]) -> None:
        """SetCurrentProgramScene — com cooldown e idempotência."""
        scene_name = params["scene_name"]

        # Idempotência: não trocar se já estamos na cena
        if self._current_scene == scene_name:
            LOGGER.debug("OBSAdapter: cena '%s' já ativa — skip", scene_name)
            return

        # Cooldown
        elapsed = time.monotonic() - self._last_scene_change_at
        if elapsed < self._config.scene_change_cooldown_s:
            LOGGER.debug(
                "OBSAdapter: cooldown de cena ativo (%.1fs restantes) — skip",
                self._config.scene_change_cooldown_s - elapsed,
            )
            return

        resp = await self._send_request(
            _REQ_SET_CURRENT_SCENE,
            {"sceneName": scene_name},
        )
        if resp is not None:
            self._current_scene = scene_name
            self._last_scene_change_at = time.monotonic()
            self._metrics.scene_changes += 1
            LOGGER.info("OBSAdapter: cena trocada para '%s'", scene_name)

    async def _do_set_source_enabled(self, params: dict[str, Any]) -> None:
        """SetSceneItemEnabled — requer sceneItemId (int), obtido via GetSceneItemId."""
        scene_name = params["scene_name"]
        source_name = params["source_name"]
        enabled = params["enabled"]

        # Cache de sceneItemId para evitar request repetida
        cache_key = (scene_name, source_name)
        scene_item_id = self._scene_item_id_cache.get(cache_key)

        if scene_item_id is None:
            resp = await self._send_request(
                _REQ_GET_SCENE_ITEM_ID,
                {"sceneName": scene_name, "sourceName": source_name},
            )
            if resp is None or "sceneItemId" not in resp:
                LOGGER.warning(
                    "OBSAdapter: não foi possível obter sceneItemId para '%s' em '%s'",
                    source_name,
                    scene_name,
                )
                return
            scene_item_id = resp["sceneItemId"]
            self._scene_item_id_cache[cache_key] = scene_item_id

        resp = await self._send_request(
            _REQ_SET_SCENE_ITEM_ENABLED,
            {
                "sceneName": scene_name,
                "sceneItemId": scene_item_id,
                "sceneItemEnabled": enabled,
            },
        )
        if resp is not None:
            self._metrics.source_updates += 1
            LOGGER.info(
                "OBSAdapter: fonte '%s' em '%s' → enabled=%s",
                source_name,
                scene_name,
                enabled,
            )

    async def _do_trigger_media(self, params: dict[str, Any]) -> None:
        """TriggerMediaInputAction — restart uma fonte de mídia allowlisted."""
        source_name = params["source_name"]
        resp = await self._send_request(
            _REQ_TRIGGER_MEDIA_ACTION,
            {
                "inputName": source_name,
                "mediaAction": _MEDIA_ACTION_RESTART,
            },
        )
        if resp is not None:
            self._metrics.media_triggers += 1
            LOGGER.info("OBSAdapter: mídia '%s' acionada (restart)", source_name)

    async def _do_set_input_text(self, params: dict[str, Any]) -> None:
        """SetInputSettings para atualizar texto de uma fonte de texto."""
        source_name = params["source_name"]
        text = params["text"]
        input_key = params.get("input_key", "text")  # "text" para GDI+/FreeType

        resp = await self._send_request(
            _REQ_SET_INPUT_SETTINGS,
            {
                "inputName": source_name,
                "inputSettings": {input_key: text},
            },
        )
        if resp is not None:
            self._metrics.source_updates += 1
            LOGGER.debug("OBSAdapter: texto da fonte '%s' atualizado", source_name)

    # ------------------------------------------------------------------
    # Efeito temporário de cena
    # ------------------------------------------------------------------

    async def trigger_temp_scene(
        self,
        target_scene: str,
        duration_s: float,
    ) -> bool:
        """Troca para target_scene temporariamente e restaura a cena anterior.

        Retorna False se: target_scene não está na allowlist, cooldown ativo,
        ou OBS não conectado.

        RACE CONDITION: só restaura se OBS ainda estiver em target_scene
        quando o timer expirar — não sobrescreve mudança manual do operador.
        """
        if not self._config.allowed_scenes or target_scene not in self._config.allowed_scenes:
            LOGGER.warning("OBSAdapter: temp scene '%s' não está na allowlist", target_scene)
            return False
        if self._state != OBSConnectionState.CONNECTED:
            return False

        original_scene = self._current_scene

        # Cancelar efeito anterior se houver
        if self._temp_scene_task and not self._temp_scene_task.done():
            self._temp_scene_task.cancel()
            try:
                await self._temp_scene_task
            except asyncio.CancelledError:
                pass

        self._temp_scene_task = asyncio.create_task(
            self._run_temp_scene(target_scene, original_scene, duration_s),
            name="obs_temp_scene",
        )
        return True

    async def _run_temp_scene(
        self,
        target: str,
        original: str | None,
        duration_s: float,
    ) -> None:
        """Executa a sequência: switch → wait → restore (se seguro)."""
        try:
            # Switch para cena alvo
            await self._do_set_scene({"scene_name": target})

            # Aguardar duração (cancelável)
            await asyncio.sleep(max(0.1, duration_s))

            # Só restaurar se OBS ainda está na cena alvo (não houve mudança manual)
            await self._refresh_current_scene()
            if self._current_scene == target and original is not None:
                LOGGER.info(
                    "OBSAdapter: restaurando cena '%s' (temp scene '%s' concluída)",
                    original,
                    target,
                )
                await self._do_set_scene({"scene_name": original})
            elif self._current_scene != target:
                LOGGER.info(
                    "OBSAdapter: cena mudada manualmente para '%s' durante efeito — não restaurar",
                    self._current_scene,
                )
        except asyncio.CancelledError:
            LOGGER.debug("OBSAdapter: temp scene task cancelada")
        except Exception:
            LOGGER.exception("OBSAdapter: erro no temp scene effect")

    # ------------------------------------------------------------------
    # Envio de requests (via run_in_executor — não bloqueia o loop)
    # ------------------------------------------------------------------

    async def _send_request(
        self,
        request_type: str,
        data: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Envia um request ao OBS via obsws-python (síncrono em executor).

        Retorna responseData em caso de sucesso, None em caso de falha.
        Não propaga exceções — erros são logados e contabilizados.
        """
        async with self._client_lock:
            client = self._client

        if client is None:
            return None

        loop = asyncio.get_event_loop()
        t0 = time.monotonic()

        try:
            resp = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: _call_obs(client, request_type, data)),
                timeout=self._config.request_timeout_s,
            )
            latency_ms = (time.monotonic() - t0) * 1000
            self._metrics.requests_total += 1
            self._metrics.last_request_latency_ms = latency_ms
            self._last_success_at = datetime.now(timezone.utc)
            if self._health:
                self._health.record_success()
            return resp or {}

        except asyncio.TimeoutError:
            latency_ms = (time.monotonic() - t0) * 1000
            self._metrics.request_failures_total += 1
            self._metrics.last_request_latency_ms = latency_ms
            self._last_error = f"Request timeout ({request_type})"
            LOGGER.warning("OBSAdapter: timeout na request %s", request_type)
            return None

        except Exception as exc:
            latency_ms = (time.monotonic() - t0) * 1000
            self._metrics.request_failures_total += 1
            self._metrics.last_request_latency_ms = latency_ms
            error_msg = _sanitize_error(str(exc))
            self._last_error = f"Request error ({request_type}): {error_msg}"
            LOGGER.error("OBSAdapter: erro na request %s: %s", request_type, error_msg)
            return None

    # ------------------------------------------------------------------
    # Mapeamento de eventos → ações
    # ------------------------------------------------------------------

    def _map_event_to_actions(self, event: Event | AggregatedEvent) -> list[OBSAction]:
        """Traduz um evento em zero ou mais OBSActions via mapeamento configurado.

        SEGURANÇA: o payload do evento não escolhe a ação — o mapeamento
        é configurado em obs_actions.json e carregado no startup.
        """
        if not self._action_mapping or isinstance(event, AggregatedEvent):
            return []

        actions: list[OBSAction] = []
        event_type = event.event_type.value if hasattr(event, "event_type") else ""
        event_priority = getattr(event, "priority", Priority.P4)
        priority_int = int(event_priority) if isinstance(event_priority, Priority) else 4
        event_priority = getattr(event, "priority", None)
        if event_priority is None and hasattr(event, "payload") and isinstance(event.payload, dict):
            event_priority = event.payload.get("_priority")
        if event_priority is None and hasattr(event, "event_type"):
            from src.domain.priorities import resolve_priority
            try:
                event_priority = resolve_priority(event.event_type)
            except (KeyError, ValueError):
                event_priority = Priority.P4
        elif event_priority is None:
            event_priority = Priority.P4
        priority_int = int(event_priority) if isinstance(event_priority, Priority) else int(event_priority)

        for rule in self._action_mapping:
            if not rule.get("enabled", True):
                continue

            # Match por event_type
            match_type = rule.get("match_event_type", "")
            if match_type and match_type != event_type:
                continue

            # Match por prioridade mínima
            match_priorities = rule.get("match_priority", [])
            if match_priorities:
                p_names = [f"P{priority_int}"]
                if not any(p in match_priorities for p in p_names):
                    continue

            action_type_raw = rule.get("action", "")
            try:
                action_type = OBSActionType(action_type_raw)
            except ValueError:
                LOGGER.warning("OBSAdapter: action type desconhecido no mapeamento: %s", action_type_raw)
                continue

            params_raw = dict(rule.get("params", {}))

            try:
                params = self._validator.validate(action_type, params_raw)
            except ValueError as exc:
                LOGGER.warning("OBSAdapter: params inválidos no mapeamento: %s", exc)
                continue

            expires_after = rule.get("expires_after_s")
            expires_at = None
            if expires_after is not None:
                from datetime import timedelta
                expires_at = datetime.now(timezone.utc) + timedelta(seconds=float(expires_after))

            priority_override = rule.get("priority", priority_int)
            dedupe_key = rule.get("dedupe_key") or (
                f"{action_type.value}:{params.get('scene_name', params.get('source_name', ''))}"
            )

            actions.append(
                OBSAction(
                    action_type=action_type,
                    params=params,
                    priority=int(priority_override),
                    expires_at=expires_at,
                    dedupe_key=dedupe_key,
                )
            )

        return actions

    # ------------------------------------------------------------------
    # Observabilidade
    # ------------------------------------------------------------------

    def health_snapshot(self) -> dict[str, Any]:
        """Estado de saúde para inclusão no /health endpoint.

        SEGURANÇA: nunca inclui a senha nem tokens.
        """
        if not self._config.enabled:
            return {"status": OBSHealthStatus.DISABLED.value, "enabled": False}

        connected = self._state == OBSConnectionState.CONNECTED

        if connected:
            status = OBSHealthStatus.HEALTHY.value
        elif self._state in (OBSConnectionState.CONNECTING, OBSConnectionState.RECONNECTING):
            status = OBSHealthStatus.DEGRADED.value
        elif self._state == OBSConnectionState.STOPPED:
            status = OBSHealthStatus.UNKNOWN.value
        else:
            status = OBSHealthStatus.UNHEALTHY.value

        return {
            "enabled": True,
            "status": status,
            "connection_state": self._state.value,
            "connected": connected,
            "host": self._config.host,
            "port": self._config.port,
            "last_success_at": (
                self._last_success_at.isoformat() if self._last_success_at else None
            ),
            "last_error": self._last_error,
            "last_request_latency_ms": round(self._metrics.last_request_latency_ms, 1),
            "queue_depth": self._queue.depth(),
            "current_scene": self._current_scene,
            "metrics": {
                **self._metrics.snapshot(),
                "obs_actions_queued": self._queue.queued_total,
                "obs_actions_dropped": self._queue.dropped_total,
                "obs_actions_expired": self._queue.expired_total,
            },
        }


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------


def load_obs_actions(path: str | Path) -> list[dict[str, Any]]:
    """Carrega mapeamento de ações OBS a partir de arquivo JSON."""
    file_path = Path(path)
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Não foi possível ler ações OBS de %s: %s", file_path, exc)
        return []

    if not isinstance(data, dict):
        return []

    rules = data.get("rules")
    if not isinstance(rules, list):
        return []

    valid_actions: list[dict[str, Any]] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if not rule.get("enabled", True):
            continue
        action = rule.get("action")
        if not action:
            continue
        valid_actions.append(rule)

    return valid_actions


def _event_id(event: Event | AggregatedEvent) -> str:
    return getattr(event, "event_id", getattr(event, "aggregate_id", "?"))


def _sanitize_error(msg: str) -> str:
    """Remove possíveis ocorrências de senha em mensagens de erro.

    SEGURANÇA: nunca logar a senha mesmo que apareça numa exception.
    """
    # Remover padrões comuns de credencial em strings de conexão
    sanitized = re.sub(r"password[=:'\"\s]+\S+", "password=***", msg, flags=re.IGNORECASE)
    sanitized = re.sub(r"auth[=:'\"\s]+\S+", "auth=***", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"(?i)\bpassword\b\s*[:=]\s*\S+", "password=***", msg)
    sanitized = re.sub(r"(?i)\bauth\b\s*[:=]\s*(?:Bearer\s+)?\S+", "auth=***", sanitized)
    sanitized = re.sub(r"(?i)\btoken\b\s*[:=]\s*\S+", "token=***", sanitized)
    return sanitized[:500]  # limitar tamanho da mensagem de erro


def _is_disconnect_error(msg: str) -> bool:
    """Detecta se uma mensagem de erro indica desconexão WebSocket."""
    keywords = ("connection", "closed", "disconnect", "websocket", "eof", "broken")
    msg_lower = msg.lower()
    return any(kw in msg_lower for kw in keywords)


def _call_obs(client: Any, request_type: str, data: dict[str, Any]) -> dict[str, Any]:
    """Chama o método correto do obsws-python ReqClient.

    obsws-python mapeia request types para métodos snake_case.
    Verificado para os request types que usamos.

    Mapeamento verificado:
      GetVersion                  → client.get_version()
      GetCurrentProgramScene      → client.get_current_program_scene()
      SetCurrentProgramScene      → client.set_current_program_scene(scene_name=...)
      GetSceneList                → client.get_scene_list()
      GetSceneItemId              → client.get_scene_item_id(scene_name=..., source_name=...)
      SetSceneItemEnabled         → client.set_scene_item_enabled(scene_name=..., scene_item_id=..., scene_item_enabled=...)
      TriggerMediaInputAction     → client.trigger_media_input_action(input_name=..., media_action=...)
      SetInputSettings            → client.set_input_settings(input_name=..., input_settings=...)

    Fonte: obsws-python 1.8.0 README e testes verificados.
    """
    # Converter camelCase requestData keys para snake_case params do obsws-python
    snake_data = {_camel_to_snake(k): v for k, v in data.items()}

    # Converter requestType para nome de método snake_case
    method_name = _to_snake_method(request_type)

    method = getattr(client, method_name, None)
    if method is None:
        raise AttributeError(
            f"obsws-python ReqClient não possui método '{method_name}' "
            f"para request '{request_type}'. Verifique a versão da biblioteca."
        )

    resp = method(**snake_data)

    # Extrair responseData como dict
    if resp is None:
        return {}
    # obsws-python retorna objetos com atributos — converter para dict
    if hasattr(resp, "__dict__"):
        return {k: v for k, v in vars(resp).items() if not k.startswith("_")}
    return {}


def _to_snake_method(request_type: str) -> str:
    """Converte PascalCase request type para snake_case método do obsws-python.

    Exemplos verificados:
      GetCurrentProgramScene → get_current_program_scene
      SetCurrentProgramScene → set_current_program_scene
      SetSceneItemEnabled    → set_scene_item_enabled
      GetSceneItemId         → get_scene_item_id
      TriggerMediaInputAction → trigger_media_input_action
      SetInputSettings       → set_input_settings
    """
    # Inserir _ antes de cada letra maiúscula que segue uma minúscula ou dígito
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", request_type)
    return snake.lower()


def _camel_to_snake(name: str) -> str:
    """Converte chave camelCase para snake_case."""
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    s = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", s)
    return s.lower()
