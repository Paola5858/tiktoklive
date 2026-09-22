"""Composição da aplicação — entry point principal do TikTok × Roblox Live Engine.

Inicializa e conecta todos os componentes:
- TikTokLiveConnector (ingestão)
- EventProcessor (engine core: dedupe, prioridade, agregação, fila, dispatch)
- RobloxBridge + Local API (entrega ao Roblox via HTTP polling)
- InteractionRuleEngine + InteractionConsumer (regras declarativas → GameEvents)
- OBSAdapter (automação OBS desacoplada)
- MQTTAdapter (IoT/ESP32 desacoplado)
- Watchdog + OperationalSnapshot (observabilidade unificada)
- Graceful shutdown coordenado (SIGTERM/SIGINT)
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI

from src.adapters.local_api import create_app as create_local_api_app
from src.adapters.mqtt import MQTTAdapter, MQTTConfig
from src.adapters.obs import OBSAdapter, OBSConfig, load_obs_actions
from src.adapters.roblox import RobloxBridge, RobloxBridgeConfig
from src.config import Settings, ConfigurationError
from src.engine.config import EngineConfig
from src.engine.dispatcher import Dispatcher
from src.engine.metrics import EngineMetrics
from src.engine.processor import EventProcessor
from src.ingestion.tiktok import TikTokLiveConnector
from src.interaction.config import load_rules_file
from src.interaction.consumer import InteractionConsumer
from src.interaction.engine import InteractionRuleEngine
from src.logging import setup_logging, get_logger
from src.observability.audit import EventAuditLogger
from src.observability.health import Watchdog
from src.observability.metrics import OperationalSnapshot
from src.observability.resilience import ResilienceMetrics

LOGGER = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Configuração completa da aplicação, agregando todas as sub-configs."""

    settings: Settings
    engine: EngineConfig
    roblox_bridge: RobloxBridgeConfig
    obs: OBSConfig
    mqtt: MQTTConfig
    tiktok_unique_id: str
    tiktok_max_buffer_size: int = 1000
    tiktok_max_reconnect_attempts: int = 5
    tiktok_backoff_delays: tuple[float, ...] = (2.0, 4.0, 8.0, 16.0, 30.0)
    local_api_host: str = "127.0.0.1"
    local_api_port: int = 8787
    interaction_rules_path: str = "configs/interaction_rules.json"
    obs_actions_path: str = "configs/obs_actions.json"
    audit_log_dir: str = "logs/events"
    shutdown_timeout: float = 10.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "AppConfig":
        source = env if env is not None else os.environ

        settings = Settings.from_env(source)

        engine = EngineConfig(
            queue_capacity_per_level=int(source.get("ENGINE_QUEUE_CAPACITY_PER_LEVEL", "500")),
            p0_express_capacity=int(source.get("ENGINE_P0_EXPRESS_CAPACITY", "100")),
            n_workers=int(source.get("ENGINE_N_WORKERS", "4")),
            max_in_flight=int(source.get("ENGINE_MAX_IN_FLIGHT", "16")),
            dedup_maxsize=int(source.get("ENGINE_DEDUP_MAXSIZE", "10000")),
            dedup_ttl_seconds=float(source.get("ENGINE_DEDUP_TTL_SECONDS", "30.0")),
            aggregation_window_seconds=float(source.get("ENGINE_AGGREGATION_WINDOW_SECONDS", "2.0")),
            aggregation_max_bucket_size=int(source.get("ENGINE_AGGREGATION_MAX_BUCKET_SIZE", "50")),
            overflow_threshold_p1=float(source.get("ENGINE_OVERFLOW_THRESHOLD_P1", "0.90")),
            overflow_threshold_p2=float(source.get("ENGINE_OVERFLOW_THRESHOLD_P2", "0.80")),
            overflow_threshold_p3=float(source.get("ENGINE_OVERFLOW_THRESHOLD_P3", "0.60")),
            overflow_threshold_p4=float(source.get("ENGINE_OVERFLOW_THRESHOLD_P4", "0.50")),
            shutdown_drain_timeout_seconds=float(source.get("ENGINE_SHUTDOWN_DRAIN_TIMEOUT_SECONDS", "5.0")),
            latency_sample_size=int(source.get("ENGINE_LATENCY_SAMPLE_SIZE", "1000")),
            log_every_n_drops=int(source.get("ENGINE_LOG_EVERY_N_DROPS", "50")),
        )

        roblox_bridge = RobloxBridgeConfig(
            buffer_capacity=int(source.get("ROBLOX_BRIDGE_BUFFER_CAPACITY", "500")),
            max_events_per_poll=int(source.get("ROBLOX_BRIDGE_MAX_EVENTS_PER_POLL", "100")),
            default_events_per_poll=int(source.get("ROBLOX_BRIDGE_DEFAULT_EVENTS_PER_POLL", "25")),
        )

        obs = OBSConfig.from_env(source)
        mqtt = MQTTConfig.from_env(source)

        tiktok_unique_id = source.get("TIKTOK_UNIQUE_ID", "").strip().lstrip("@")
        if not tiktok_unique_id:
            raise ConfigurationError("TIKTOK_UNIQUE_ID é obrigatório (ex: @usuario ou usuario)")

        return cls(
            settings=settings,
            engine=engine,
            roblox_bridge=roblox_bridge,
            obs=obs,
            mqtt=mqtt,
            tiktok_unique_id=tiktok_unique_id,
            tiktok_max_buffer_size=int(source.get("TIKTOK_MAX_BUFFER_SIZE", "1000")),
            tiktok_max_reconnect_attempts=int(source.get("TIKTOK_MAX_RECONNECT_ATTEMPTS", "5")),
            tiktok_backoff_delays=tuple(
                float(x.strip()) for x in source.get("TIKTOK_BACKOFF_DELAYS", "2,4,8,16,30").split(",") if x.strip()
            ),
            local_api_host=source.get("LOCAL_API_HOST", "127.0.0.1"),
            local_api_port=int(source.get("LOCAL_API_PORT", "8787")),
            interaction_rules_path=source.get("INTERACTION_RULES_PATH", "configs/interaction_rules.json"),
            obs_actions_path=source.get("OBS_ACTIONS_PATH", "configs/obs_actions.json"),
            audit_log_dir=source.get("AUDIT_LOG_DIR", "logs/events"),
            shutdown_timeout=float(source.get("SHUTDOWN_TIMEOUT", "10.0")),
        )


class LiveEngineApp:
    """Aplicação principal orquestrando todos os componentes."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._shutdown_event = asyncio.Event()
        self._started = False

        # Core observabilidade
        self._resilience = ResilienceMetrics()
        self._watchdog = Watchdog(timeout_seconds=30.0, resilience=self._resilience)
        self._audit_logger = EventAuditLogger(log_dir=config.audit_log_dir)
        self._engine_metrics = EngineMetrics()

        # Componentes principais
        self._dispatcher: Dispatcher | None = None
        self._processor: EventProcessor | None = None
        self._roblox_bridge: RobloxBridge | None = None
        self._interaction_engine: InteractionRuleEngine | None = None
        self._interaction_consumer: InteractionConsumer | None = None
        self._obs_adapter: OBSAdapter | None = None
        self._mqtt_adapter: MQTTAdapter | None = None
        self._tiktok_connector: TikTokLiveConnector | None = None
        self._tiktok_consumer_task: asyncio.Task[None] | None = None
        self._local_api_app: FastAPI | None = None
        self._local_api_server: uvicorn.Server | None = None
        self._local_api_task: asyncio.Task[None] | None = None
        self._operational_snapshot: OperationalSnapshot | None = None

    async def start(self) -> None:
        """Inicializa todos os componentes e inicia o pipeline completo."""
        if self._started:
            LOGGER.warning("App já iniciado")
            return

        LOGGER.info("Iniciando TikTok × Roblox Live Engine...")
        LOGGER.info("TikTok target: @%s", self._config.tiktok_unique_id)
        LOGGER.info("Local API: http://%s:%d", self._config.local_api_host, self._config.local_api_port)
        LOGGER.info("OBS: %s", "habilitado" if self._config.obs.enabled else "desabilitado")
        LOGGER.info("MQTT: %s", "habilitado" if self._config.mqtt.enabled else "desabilitado")

        # 1. Dispatcher (hub de consumers)
        self._dispatcher = Dispatcher(self._engine_metrics)

        # 2. Roblox Bridge (consumer principal)
        self._roblox_bridge = RobloxBridge(
            config=self._config.roblox_bridge,
            watchdog=self._watchdog,
        )
        self._dispatcher.register(self._roblox_bridge)

        # 3. Interaction Rule Engine + Consumer
        rules = load_rules_file(self._config.interaction_rules_path)
        self._interaction_engine = InteractionRuleEngine(rules)
        self._interaction_consumer = InteractionConsumer(
            engine=self._interaction_engine,
            bridges=[self._roblox_bridge],
        )
        self._dispatcher.register(self._interaction_consumer)

        # 4. OBS Adapter (opcional)
        if self._config.obs.enabled:
            obs_actions = load_obs_actions(self._config.obs_actions_path)
            self._obs_adapter = OBSAdapter(
                config=self._config.obs,
                watchdog=self._watchdog,
                action_mapping=obs_actions,
                resilience=self._resilience,
            )
            self._dispatcher.register(self._obs_adapter)

        # 5. MQTT Adapter (opcional)
        if self._config.mqtt.enabled:
            self._mqtt_adapter = MQTTAdapter(
                config=self._config.mqtt,
                watchdog=self._watchdog,
                resilience=self._resilience,
            )
            # MQTT consome GameEvents via InteractionConsumer.bridges
            # O InteractionConsumer já publica no RobloxBridge, adicionamos MQTT como bridge extra
            if self._interaction_consumer:
                self._interaction_consumer.bridges.append(self._mqtt_adapter)

        # 6. Event Processor (core pipeline)
        self._processor = EventProcessor(
            config=self._config.engine,
            metrics=self._engine_metrics,
            dispatcher=self._dispatcher,
            watchdog=self._watchdog,
            audit_logger=self._audit_logger,
            resilience=self._resilience,
        )

        # 7. Operational Snapshot (para /health)
        self._operational_snapshot = OperationalSnapshot(
            metrics=self._engine_metrics,
            watchdog=self._watchdog,
            resilience=self._resilience,
        )

        # 8. Local API (FastAPI)
        self._local_api_app = create_local_api_app(
            bridge=self._roblox_bridge,
            snapshot=self._operational_snapshot,
        )

        # 9. TikTok Connector
        self._tiktok_connector = TikTokLiveConnector(
            unique_id=self._config.tiktok_unique_id,
            max_buffer_size=self._config.tiktok_max_buffer_size,
            max_reconnect_attempts=self._config.tiktok_max_reconnect_attempts,
            backoff_delays=self._config.tiktok_backoff_delays,
            watchdog=self._watchdog,
        )

        # Iniciar componentes na ordem correta
        await self._audit_logger.start()
        await self._processor.start()

        if self._obs_adapter:
            await self._obs_adapter.start()

        if self._mqtt_adapter:
            await self._mqtt_adapter.start()

        # Iniciar Local API em background
        config = uvicorn.Config(
            self._local_api_app,
            host=self._config.local_api_host,
            port=self._config.local_api_port,
            log_level=self._config.settings.log_level.lower(),
            access_log=False,
        )
        self._local_api_server = uvicorn.Server(config)
        self._local_api_task = asyncio.create_task(
            self._local_api_server.serve(),
            name="local_api_server",
        )

        # Iniciar consumidor TikTok
        self._tiktok_consumer_task = asyncio.create_task(
            self._consume_tiktok_events(),
            name="tiktok_event_consumer",
        )

        self._started = True
        LOGGER.info("Live Engine iniciado com sucesso")

    async def _consume_tiktok_events(self) -> None:
        """Consome eventos do TikTok e alimenta o EventProcessor."""
        if not self._tiktok_connector or not self._processor:
            return

        connector_task = self._tiktok_connector.start()
        try:
            async for event in self._tiktok_connector.events():
                if self._shutdown_event.is_set():
                    break
                try:
                    await self._processor.receive(event)
                except Exception as exc:
                    LOGGER.error("Falha ao processar evento TikTok: %s", exc)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            LOGGER.exception("Erro no consumidor TikTok: %s", exc)
        finally:
            if not connector_task.done():
                await self._tiktok_connector.stop()

    async def stop(self) -> None:
        """Shutdown coordenado e gracioso de todos os componentes."""
        if not self._started:
            return

        LOGGER.info("Iniciando shutdown gracioso...")
        self._shutdown_event.set()

        # 1. Parar consumidor TikTok
        if self._tiktok_consumer_task and not self._tiktok_consumer_task.done():
            self._tiktok_consumer_task.cancel()
            try:
                await asyncio.wait_for(self._tiktok_consumer_task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        if self._tiktok_connector:
            try:
                await self._tiktok_connector.stop()
            except asyncio.CancelledError:
                pass

        # 2. Parar Event Processor (drain P0 + workers)
        if self._processor:
            await self._processor.stop()

        # 3. Parar adapters externos
        if self._obs_adapter:
            await self._obs_adapter.stop()

        if self._mqtt_adapter:
            await self._mqtt_adapter.stop()

        # 4. Parar Local API
        if self._local_api_server:
            self._local_api_server.should_exit = True
        if self._local_api_task and not self._local_api_task.done():
            try:
                await asyncio.wait_for(self._local_api_task, timeout=3.0)
            except asyncio.TimeoutError:
                self._local_api_task.cancel()
                try:
                    await self._local_api_task
                except asyncio.CancelledError:
                    pass

        # 5. Parar audit logger
        await self._audit_logger.stop()

        self._started = False
        LOGGER.info("Shutdown completo")

    @property
    def is_running(self) -> bool:
        return self._started and not self._shutdown_event.is_set()


async def _run_app(config: AppConfig) -> None:
    """Executa a aplicação com tratamento de sinais."""
    app = LiveEngineApp(config)

    loop = asyncio.get_running_loop()
    shutdown_triggered = asyncio.Event()

    def _signal_handler(signame: str) -> None:
        LOGGER.info("Sinal %s recebido, iniciando shutdown...", signame)
        shutdown_triggered.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda s=sig: _signal_handler(s.name))
        except NotImplementedError:
            # Windows não suporta add_signal_handler para todos os sinais
            pass

    try:
        await app.start()

        # Aguardar sinal de shutdown ou erro
        await shutdown_triggered.wait()

    except Exception as exc:
        LOGGER.exception("Erro fatal na aplicação: %s", exc)
        raise
    finally:
        await app.stop()


def _load_env_file() -> None:
    """Carrega `.env` do repositório, se existir, sem sobrescrever variáveis já definidas.

    Faz o entrypoint funcionar sozinho (`python -m src.app`) sem depender
    de o usuário exportar as variáveis manualmente ou de python-dotenv.
    """
    candidates = [
        Path(__file__).resolve().parent.parent / ".env",
        Path.cwd() / ".env",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
        except OSError:
            pass
        break


def main() -> None:
    """Entry point principal."""
    _load_env_file()
    try:
        config = AppConfig.from_env()
    except ConfigurationError as exc:
        print(f"Erro de configuração: {exc}", file=sys.stderr)
        sys.exit(1)

    setup_logging(level=config.settings.log_level)

    try:
        asyncio.run(_run_app(config))
    except KeyboardInterrupt:
        pass  # Já tratado pelo signal handler
    except Exception as exc:
        LOGGER.exception("Aplicação terminou com erro: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()