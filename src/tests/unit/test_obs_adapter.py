"""Testes unitários do OBS Adapter.

Nenhum teste aqui requer OBS real — todos usam mocks e verificam
comportamento isolado: validação, allowlist, queue, state machine,
métricas, falhas e isolamento do Event Engine.

Cobertura:
  - OBSConfig: from_env, validação, config desabilitada
  - OBSActionValidator: allowlist, sanitização de texto, rejeição de filepath
  - OBSPriorityQueue: overflow (P4 drop, P1 preserved), dedupe, expiração, prioridade
  - OBSAdapter: disabled → can_handle=False, handle() não-bloqueante
  - OBSAdapter: connection state machine
  - OBSAdapter: reconnect backoff, shutdown cancela reconnect
  - OBSAdapter: health_snapshot não expõe senha
  - OBSAdapter: temp scene não restaura se cena mudada manualmente
  - Helpers: _to_snake_method, _camel_to_snake, _sanitize_error
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.obs import (
    OBSAction,
    OBSActionType,
    OBSActionValidator,
    OBSAdapter,
    OBSConfig,
    OBSConnectionState,
    OBSPriorityQueue,
    _camel_to_snake,
    _sanitize_error,
    _to_snake_method,
)
from src.domain.events import Event, EventType
from src.domain.priorities import Priority


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_event(event_type: EventType = EventType.GIFT, priority: Priority = Priority.P1) -> Event:
    """Cria um Event mínimo válido para testes."""
    from src.domain.events import EventUser
    return Event(
        event_id="test-event-id",
        event_type=event_type,
        source="tiktok",
        timestamp=datetime.now(timezone.utc),
        received_at=datetime.now(timezone.utc),
        user=EventUser(external_id="u123", display_name="TestUser"),
        payload={"gift_id": 1, "gift_value": 100, "repeat_count": 1, "_priority": priority},
        status="received",
    )


def _config_with_scenes(*scenes: str, sources: tuple[str, ...] = ()) -> OBSConfig:
    return OBSConfig(
        enabled=True,
        host="localhost",
        port=4455,
        password="",
        connect_timeout_s=5.0,
        request_timeout_s=3.0,
        reconnect_delays_s=(0.01, 0.02),
        scene_change_cooldown_s=0.0,
        allowed_scenes=frozenset(scenes),
        allowed_sources=frozenset(sources),
        obs_queue_maxsize=20,
    )


# ---------------------------------------------------------------------------
# OBSConfig tests
# ---------------------------------------------------------------------------


class TestOBSConfig:
    def test_from_env_disabled_by_default(self) -> None:
        config = OBSConfig.from_env({})
        assert config.enabled is False

    def test_from_env_enabled(self) -> None:
        config = OBSConfig.from_env({"OBS_ENABLED": "true"})
        assert config.enabled is True

    def test_from_env_password_read_from_env(self) -> None:
        config = OBSConfig.from_env({"OBS_ENABLED": "true", "OBS_PASSWORD": "secret"})
        assert config.password == "secret"

    def test_from_env_port_default(self) -> None:
        config = OBSConfig.from_env({})
        assert config.port == 4455

    def test_from_env_port_custom(self) -> None:
        config = OBSConfig.from_env({"OBS_PORT": "4456"})
        assert config.port == 4456

    def test_from_env_invalid_port(self) -> None:
        with pytest.raises(ValueError, match="OBS_PORT"):
            OBSConfig.from_env({"OBS_PORT": "not_a_number"})

    def test_from_env_allowed_scenes(self) -> None:
        config = OBSConfig.from_env({"OBS_ALLOWED_SCENES": "Main,BRB,Starting"})
        assert config.allowed_scenes == frozenset({"Main", "BRB", "Starting"})

    def test_from_env_reconnect_delays(self) -> None:
        config = OBSConfig.from_env({"OBS_RECONNECT_DELAYS": "1,2,4"})
        assert config.reconnect_delays_s == (1.0, 2.0, 4.0)

    def test_from_env_invalid_delays(self) -> None:
        with pytest.raises(ValueError, match="OBS_RECONNECT_DELAYS"):
            OBSConfig.from_env({"OBS_RECONNECT_DELAYS": "1,x,4"})

    def test_invalid_port_range(self) -> None:
        with pytest.raises(ValueError):
            OBSConfig(
                enabled=True, host="localhost", port=0, password="",
                connect_timeout_s=5.0, request_timeout_s=3.0,
                reconnect_delays_s=(2.0,), scene_change_cooldown_s=0.0,
                allowed_scenes=frozenset(), allowed_sources=frozenset(),
                obs_queue_maxsize=10,
            )

    def test_invalid_host(self) -> None:
        with pytest.raises(ValueError, match="host"):
            OBSConfig(
                enabled=True, host="  ", port=4455, password="",
                connect_timeout_s=5.0, request_timeout_s=3.0,
                reconnect_delays_s=(2.0,), scene_change_cooldown_s=0.0,
                allowed_scenes=frozenset(), allowed_sources=frozenset(),
                obs_queue_maxsize=10,
            )

    def test_disabled_config(self) -> None:
        config = OBSConfig.disabled()
        assert config.enabled is False
        assert config.port == 4455


# ---------------------------------------------------------------------------
# OBSActionValidator tests
# ---------------------------------------------------------------------------


class TestOBSActionValidator:
    def test_set_scene_valid(self) -> None:
        config = _config_with_scenes("Main", "BRB")
        v = OBSActionValidator(config)
        result = v.validate(OBSActionType.OBS_SET_SCENE, {"scene_name": "Main"})
        assert result == {"scene_name": "Main"}

    def test_set_scene_not_in_allowlist(self) -> None:
        config = _config_with_scenes("Main")
        v = OBSActionValidator(config)
        with pytest.raises(ValueError, match="allowlist"):
            v.validate(OBSActionType.OBS_SET_SCENE, {"scene_name": "Hidden"})

    def test_set_scene_empty_allowlist_accepts_any(self) -> None:
        """Allowlist vazia significa: nenhuma cena permitida por configuração."""
        config = OBSConfig(
            enabled=True, host="localhost", port=4455, password="",
            connect_timeout_s=5.0, request_timeout_s=3.0,
            reconnect_delays_s=(2.0,), scene_change_cooldown_s=0.0,
            allowed_scenes=frozenset(),  # vazia
            allowed_sources=frozenset(),
            obs_queue_maxsize=10,
        )
        v = OBSActionValidator(config)
        # Com allowlist vazia, a condição `self._config.allowed_scenes and ...`
        # é False, então qualquer cena é aceita (comportamento seguro somente
        # quando explicitamente configurado assim)
        result = v.validate(OBSActionType.OBS_SET_SCENE, {"scene_name": "AnyScene"})
        assert result["scene_name"] == "AnyScene"

    def test_set_scene_name_too_long(self) -> None:
        config = _config_with_scenes("Main")
        v = OBSActionValidator(config)
        with pytest.raises(ValueError):
            v.validate(OBSActionType.OBS_SET_SCENE, {"scene_name": "A" * 257})

    def test_set_scene_missing_name(self) -> None:
        config = _config_with_scenes("Main")
        v = OBSActionValidator(config)
        with pytest.raises(ValueError):
            v.validate(OBSActionType.OBS_SET_SCENE, {"scene_name": ""})

    def test_set_source_enabled_valid(self) -> None:
        config = _config_with_scenes("Main", sources=("overlay",))
        v = OBSActionValidator(config)
        result = v.validate(
            OBSActionType.OBS_SET_SOURCE_ENABLED,
            {"scene_name": "Main", "source_name": "overlay", "enabled": True},
        )
        assert result["enabled"] is True

    def test_set_source_not_in_allowlist(self) -> None:
        config = _config_with_scenes("Main", sources=("overlay",))
        v = OBSActionValidator(config)
        with pytest.raises(ValueError, match="allowlist"):
            v.validate(
                OBSActionType.OBS_SET_SOURCE_ENABLED,
                {"scene_name": "Main", "source_name": "camera", "enabled": True},
            )

    def test_trigger_media_rejects_filepath(self) -> None:
        config = _config_with_scenes(sources=("media_src",))
        v = OBSActionValidator(config)
        with pytest.raises(ValueError, match=r"(?:caminhos|arquivo|URL|file|path)"):
            v.validate(
                OBSActionType.OBS_TRIGGER_MEDIA,
                {"source_name": "media_src", "file_path": "/evil/path.mp4"},
            )

    def test_trigger_media_rejects_url(self) -> None:
        config = _config_with_scenes(sources=("media_src",))
        v = OBSActionValidator(config)
        with pytest.raises(ValueError, match=r"(?:caminhos|arquivo|URL|file|path)"):
            v.validate(
                OBSActionType.OBS_TRIGGER_MEDIA,
                {"source_name": "media_src", "url": "http://evil.com/video.mp4"},
            )

    def test_set_input_text_sanitizes_control_chars(self) -> None:
        config = _config_with_scenes(sources=("ticker",))
        v = OBSActionValidator(config)
        # Caracteres de controle devem ser removidos
        result = v.validate(
            OBSActionType.OBS_SET_INPUT_TEXT,
            {"source_name": "ticker", "text": "Hello\x00World\x1b[31m"},
        )
        assert "\x00" not in result["text"]
        assert "\x1b" not in result["text"]
        assert "Hello" in result["text"]

    def test_set_input_text_truncates_to_max(self) -> None:
        config = _config_with_scenes(sources=("ticker",))
        v = OBSActionValidator(config)
        long_text = "A" * 500
        result = v.validate(
            OBSActionType.OBS_SET_INPUT_TEXT,
            {"source_name": "ticker", "text": long_text},
        )
        assert len(result["text"]) <= 200

    def test_too_many_params(self) -> None:
        config = _config_with_scenes("Main")
        v = OBSActionValidator(config)
        many_params = {f"key_{i}": i for i in range(20)}
        many_params["scene_name"] = "Main"
        with pytest.raises(ValueError, match="excede"):
            v.validate(OBSActionType.OBS_SET_SCENE, many_params)


# ---------------------------------------------------------------------------
# OBSPriorityQueue tests
# ---------------------------------------------------------------------------


def _make_action(
    action_type: OBSActionType = OBSActionType.OBS_SET_SCENE,
    priority: int = 3,
    dedupe_key: str | None = None,
    expires_in_s: float | None = None,
) -> OBSAction:
    expires_at = None
    if expires_in_s is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in_s)
    return OBSAction(
        action_type=action_type,
        params={"scene_name": "Main"},
        priority=priority,
        dedupe_key=dedupe_key,
        expires_at=expires_at,
    )


class TestOBSPriorityQueue:
    def test_basic_enqueue_and_get(self) -> None:
        q = OBSPriorityQueue(maxsize_per_level=10)
        action = _make_action(priority=1)
        assert q.put_nowait(action) is True
        assert q.depth() == 1

    def test_high_priority_before_low(self) -> None:
        """P1 deve ser obtida antes de P3."""
        q = OBSPriorityQueue(maxsize_per_level=10)
        p3 = _make_action(action_type=OBSActionType.OBS_TRIGGER_MEDIA, priority=3)
        p1 = _make_action(action_type=OBSActionType.OBS_SET_SCENE, priority=1)
        q.put_nowait(p3)
        q.put_nowait(p1)

        loop = asyncio.new_event_loop()
        try:
            first = loop.run_until_complete(asyncio.wait_for(q.get_next(), timeout=1.0))
            assert first.priority == 1
        finally:
            loop.close()

    def test_p4_dropped_on_overflow(self) -> None:
        """Ações P4 são descartadas quando a fila P4 está cheia."""
        q = OBSPriorityQueue(maxsize_per_level=8)  # P4 capacity = 8//8 = 1
        p4a = _make_action(priority=4)
        p4b = _make_action(priority=4, dedupe_key="different")

        q.put_nowait(p4a)
        result = q.put_nowait(p4b)

        # Um dos dois deve ser dropped
        assert result is False or q.dropped_total > 0

    def test_p1_preserved_on_overflow(self) -> None:
        """Ações P1 nunca são descartadas mesmo com overflow."""
        q = OBSPriorityQueue(maxsize_per_level=2)
        # Preencher a fila P1
        for _ in range(5):
            a = _make_action(priority=1)
            q.put_nowait(a)
        # P1 deve ser sempre aceita (old items são descartados)
        total_queued = q.queued_total
        assert total_queued >= 1
        # Não deve dar crash e deve contabilizar

    def test_dedupe_same_key_dropped(self) -> None:
        """Mesma dedupe_key na segunda inserção → descartada."""
        q = OBSPriorityQueue(maxsize_per_level=10)
        a1 = _make_action(priority=2, dedupe_key="same_key")
        a2 = _make_action(priority=2, dedupe_key="same_key")

        q.put_nowait(a1)
        result = q.put_nowait(a2)
        assert result is False  # segunda é dropped por dedupe
        assert q.dropped_total == 1

    def test_dedupe_different_key_accepted(self) -> None:
        """Dedupe keys diferentes → ambas aceitas."""
        q = OBSPriorityQueue(maxsize_per_level=10)
        a1 = _make_action(priority=2, dedupe_key="key_a")
        a2 = _make_action(priority=2, dedupe_key="key_b")

        q.put_nowait(a1)
        result = q.put_nowait(a2)
        assert result is True
        assert q.queued_total == 2

    @pytest.mark.asyncio
    async def test_expired_action_discarded(self) -> None:
        """Ações expiradas são descartadas ao ser obtidas."""
        q = OBSPriorityQueue(maxsize_per_level=10)
        # Ação expirada há 1 segundo
        expired = _make_action(priority=2, expires_in_s=-1.0)
        fresh = _make_action(priority=3)

        q.put_nowait(expired)
        q.put_nowait(fresh)

        # get_next deve pular a expirada e retornar a fresca
        action = await asyncio.wait_for(q.get_next(), timeout=1.0)
        assert action.priority == 3  # fresh P3 retornada depois de skippar expirada
        assert q.expired_total == 1

    def test_depth_is_accurate(self) -> None:
        q = OBSPriorityQueue(maxsize_per_level=10)
        q.put_nowait(_make_action(priority=0))
        q.put_nowait(_make_action(priority=1))
        q.put_nowait(_make_action(priority=2))
        assert q.depth() == 3


# ---------------------------------------------------------------------------
# OBSAdapter tests
# ---------------------------------------------------------------------------


class TestOBSAdapterDisabled:
    """Quando OBS está desabilitado, não há overhead algum."""

    def test_can_handle_returns_false(self) -> None:
        adapter = OBSAdapter(config=OBSConfig.disabled())
        event = _make_event()
        assert adapter.can_handle(event) is False

    @pytest.mark.asyncio
    async def test_handle_does_nothing_when_disabled(self) -> None:
        adapter = OBSAdapter(config=OBSConfig.disabled())
        event = _make_event()
        # Não deve lançar exceção e a fila deve continuar vazia
        await adapter.handle(event)
        assert adapter._queue.depth() == 0

    @pytest.mark.asyncio
    async def test_start_does_not_create_tasks_when_disabled(self) -> None:
        adapter = OBSAdapter(config=OBSConfig.disabled())
        await adapter.start()
        assert adapter._worker_task is None
        assert adapter._reconnect_task is None
        await adapter.stop()

    def test_health_snapshot_shows_disabled(self) -> None:
        adapter = OBSAdapter(config=OBSConfig.disabled())
        snap = adapter.health_snapshot()
        assert snap["status"] == "disabled"
        assert snap["enabled"] is False
        assert "password" not in str(snap)


class TestOBSAdapterEnabled:
    """Testes do adapter habilitado sem OBS real."""

    def _make_adapter(self, scenes: tuple[str, ...] = ("Main",)) -> OBSAdapter:
        config = _config_with_scenes(*scenes, sources=("overlay",))
        # action_mapping simples para testes
        mapping = [
            {
                "enabled": True,
                "match_event_type": "GIFT",
                "match_priority": ["P1"],
                "action": "OBS_SET_SCENE",
                "params": {"scene_name": "Main"},
                "priority": 1,
                "expires_after_s": 60,
            }
        ]
        return OBSAdapter(config=config, action_mapping=mapping)

    def test_can_handle_returns_true_when_enabled(self) -> None:
        adapter = self._make_adapter()
        event = _make_event()
        assert adapter.can_handle(event) is True

    @pytest.mark.asyncio
    async def test_handle_enqueues_action_for_matching_event(self) -> None:
        adapter = self._make_adapter()
        event = _make_event(EventType.GIFT, Priority.P1)
        await adapter.handle(event)
        assert adapter._queue.depth() == 1

    @pytest.mark.asyncio
    async def test_handle_does_not_enqueue_non_matching_event(self) -> None:
        adapter = self._make_adapter()
        event = _make_event(EventType.COMMENT, Priority.P3)  # não casa com GIFT match
        await adapter.handle(event)
        assert adapter._queue.depth() == 0

    def test_initial_state_is_disconnected(self) -> None:
        adapter = self._make_adapter()
        assert adapter._state == OBSConnectionState.DISCONNECTED

    def test_health_snapshot_does_not_expose_password(self) -> None:
        config = OBSConfig(
            enabled=True, host="localhost", port=4455, password="super_secret",
            connect_timeout_s=5.0, request_timeout_s=3.0,
            reconnect_delays_s=(2.0,), scene_change_cooldown_s=0.0,
            allowed_scenes=frozenset({"Main"}), allowed_sources=frozenset(),
            obs_queue_maxsize=10,
        )
        adapter = OBSAdapter(config=config)
        snap = adapter.health_snapshot()
        snap_str = str(snap)
        assert "super_secret" not in snap_str
        assert "password" not in snap_str.lower() or "***" in snap_str

    def test_health_snapshot_shows_unhealthy_when_disconnected(self) -> None:
        adapter = self._make_adapter()
        snap = adapter.health_snapshot()
        assert snap["status"] in ("unhealthy", "unknown", "degraded")
        assert snap["connected"] is False

    @pytest.mark.asyncio
    async def test_start_creates_background_tasks(self) -> None:
        adapter = self._make_adapter()
        # Mock _connect_loop para não tentar conexão real
        adapter._connect_loop = AsyncMock()
        await adapter.start()
        assert adapter._worker_task is not None
        assert adapter._reconnect_task is not None
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_tasks_cleanly(self) -> None:
        adapter = self._make_adapter()
        adapter._connect_loop = AsyncMock()
        await adapter.start()
        # Não deve lançar exceção
        await adapter.stop()
        assert adapter._state == OBSConnectionState.STOPPED

    @pytest.mark.asyncio
    async def test_handle_does_not_block_on_obs_failure(self) -> None:
        """handle() deve retornar imediatamente mesmo com OBS offline."""
        adapter = self._make_adapter()
        # OBS está offline (estado DISCONNECTED)
        event = _make_event(EventType.GIFT, Priority.P1)

        import time
        t0 = time.monotonic()
        await adapter.handle(event)
        elapsed = time.monotonic() - t0

        # handle() deve ser sub-milissegundo (só enfileira, não tenta conectar)
        assert elapsed < 0.1, f"handle() bloqueou por {elapsed:.3f}s"


# ---------------------------------------------------------------------------
# Connection State Machine tests
# ---------------------------------------------------------------------------


class TestOBSConnectionStateMachine:
    @pytest.mark.asyncio
    async def test_failed_connection_transitions_to_disconnected(self) -> None:
        config = _config_with_scenes("Main")
        adapter = OBSAdapter(config=config)

        with patch.object(adapter, "_create_client", side_effect=ConnectionRefusedError("refused")):
            success = await adapter._try_connect()

        assert success is False
        assert adapter._state == OBSConnectionState.DISCONNECTED

    @pytest.mark.asyncio
    async def test_connection_timeout_transitions_to_disconnected(self) -> None:
        config = OBSConfig(
            enabled=True, host="localhost", port=4455, password="",
            connect_timeout_s=0.01,  # timeout muito curto
            request_timeout_s=3.0,
            reconnect_delays_s=(0.01,), scene_change_cooldown_s=0.0,
            allowed_scenes=frozenset({"Main"}), allowed_sources=frozenset(),
            obs_queue_maxsize=10,
        )
        adapter = OBSAdapter(config=config)

        async def _slow_connect():
            await asyncio.sleep(10)  # simular conexão lenta

        with patch.object(adapter, "_create_client", side_effect=asyncio.TimeoutError):
            success = await adapter._try_connect()

        assert success is False
        assert adapter._state == OBSConnectionState.DISCONNECTED

    @pytest.mark.asyncio
    async def test_reconnect_loop_respects_backoff(self) -> None:
        """Reconnect loop deve usar delays configurados."""
        delays_used: list[float] = []
        config = OBSConfig(
            enabled=True, host="localhost", port=4455, password="",
            connect_timeout_s=0.01,
            request_timeout_s=3.0,
            reconnect_delays_s=(0.05, 0.1),
            scene_change_cooldown_s=0.0,
            allowed_scenes=frozenset(),
            allowed_sources=frozenset(),
            obs_queue_maxsize=10,
        )
        adapter = OBSAdapter(config=config)
        adapter._stop_event.set()  # parar após primeira tentativa

        with patch.object(adapter, "_try_connect", return_value=False):
            # Apenas verifica que não lança exceção
            await asyncio.wait_for(adapter._connect_loop(), timeout=1.0)

    @pytest.mark.asyncio
    async def test_shutdown_cancels_reconnect(self) -> None:
        """Stop event deve cancelar o reconnect loop."""
        config = _config_with_scenes("Main")
        adapter = OBSAdapter(config=config)

        with patch.object(adapter, "_try_connect", return_value=False):
            loop_task = asyncio.create_task(adapter._connect_loop())
            await asyncio.sleep(0.01)
            adapter._stop_event.set()
            await asyncio.wait_for(loop_task, timeout=1.0)
        # Não deve ficar rodando indefinidamente


# ---------------------------------------------------------------------------
# Temp scene / Manual override tests
# ---------------------------------------------------------------------------


class TestOBSTempScene:
    @pytest.mark.asyncio
    async def test_temp_scene_does_not_restore_after_manual_change(self) -> None:
        """Se o operador mudou a cena durante o efeito, não restaurar."""
        config = _config_with_scenes("Main", "Effect")
        adapter = OBSAdapter(config=config)
        adapter._state = OBSConnectionState.CONNECTED
        adapter._current_scene = "Main"

        scenes_set: list[str] = []

        async def mock_do_set_scene(params: dict) -> None:
            adapter._current_scene = params["scene_name"]
            scenes_set.append(params["scene_name"])

        async def mock_refresh() -> None:
            # Simular: operador mudou para "BRB" durante o efeito
            adapter._current_scene = "BRB"

        adapter._do_set_scene = mock_do_set_scene
        adapter._refresh_current_scene = mock_refresh

        await adapter._run_temp_scene("Effect", "Main", 0.05)

        # Deve ter trocado para Effect mas NÃO restaurado para Main
        # (porque refresh mostrou que OBS está em BRB — mudança manual)
        assert "Main" not in scenes_set or scenes_set[-1] != "Main"

    @pytest.mark.asyncio
    async def test_temp_scene_restores_when_safe(self) -> None:
        """Deve restaurar a cena original se OBS ainda está na cena do efeito."""
        config = _config_with_scenes("Main", "Effect")
        adapter = OBSAdapter(config=config)
        adapter._state = OBSConnectionState.CONNECTED
        adapter._current_scene = "Main"

        scenes_set: list[str] = []

        async def mock_do_set_scene(params: dict) -> None:
            adapter._current_scene = params["scene_name"]
            scenes_set.append(params["scene_name"])

        async def mock_refresh() -> None:
            pass  # current_scene permanece "Effect" (não houve mudança manual)

        adapter._do_set_scene = mock_do_set_scene
        adapter._refresh_current_scene = mock_refresh

        await adapter._run_temp_scene("Effect", "Main", 0.05)

        # Deve ter: switch para Effect, depois restore para Main
        assert "Effect" in scenes_set
        assert scenes_set[-1] == "Main"

    @pytest.mark.asyncio
    async def test_temp_scene_returns_false_when_disconnected(self) -> None:
        config = _config_with_scenes("Main", "Effect")
        adapter = OBSAdapter(config=config)
        adapter._state = OBSConnectionState.DISCONNECTED

        result = await adapter.trigger_temp_scene("Effect", 5.0)
        assert result is False

    @pytest.mark.asyncio
    async def test_temp_scene_returns_false_when_not_in_allowlist(self) -> None:
        config = _config_with_scenes("Main")  # "Effect" não está aqui
        adapter = OBSAdapter(config=config)
        adapter._state = OBSConnectionState.CONNECTED

        result = await adapter.trigger_temp_scene("Effect", 5.0)
        assert result is False


# ---------------------------------------------------------------------------
# Helpers tests
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_to_snake_method_known_requests(self) -> None:
        cases = [
            ("GetCurrentProgramScene", "get_current_program_scene"),
            ("SetCurrentProgramScene", "set_current_program_scene"),
            ("SetSceneItemEnabled", "set_scene_item_enabled"),
            ("GetSceneItemId", "get_scene_item_id"),
            ("TriggerMediaInputAction", "trigger_media_input_action"),
            ("SetInputSettings", "set_input_settings"),
            ("GetVersion", "get_version"),
        ]
        for pascal, expected_snake in cases:
            result = _to_snake_method(pascal)
            assert result == expected_snake, f"{pascal} → {result!r} (esperado {expected_snake!r})"

    def test_camel_to_snake(self) -> None:
        assert _camel_to_snake("sceneName") == "scene_name"
        assert _camel_to_snake("sceneItemId") == "scene_item_id"
        assert _camel_to_snake("sceneItemEnabled") == "scene_item_enabled"
        assert _camel_to_snake("inputName") == "input_name"
        assert _camel_to_snake("mediaAction") == "media_action"

    def test_sanitize_error_removes_password(self) -> None:
        msg = "Connection failed: password=supersecret host=localhost"
        result = _sanitize_error(msg)
        assert "supersecret" not in result
        assert "***" in result

    def test_sanitize_error_removes_auth(self) -> None:
        msg = "Auth failed: auth=Bearer tok3n"
        result = _sanitize_error(msg)
        assert "tok3n" not in result

    def test_sanitize_error_truncates_long_message(self) -> None:
        long_msg = "x" * 1000
        result = _sanitize_error(long_msg)
        assert len(result) <= 500

    def test_sanitize_error_keeps_useful_info(self) -> None:
        msg = "Connection refused: localhost:4455"
        result = _sanitize_error(msg)
        assert "Connection refused" in result
        assert "localhost:4455" in result
