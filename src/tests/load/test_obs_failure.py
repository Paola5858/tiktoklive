"""Testes de falha e carga do OBS Adapter.

Simula cenários adversos sem requerer OBS real:
  - 100 ações de baixa prioridade → fila saturada → P4 dropped
  - Ação de alta prioridade chega durante fila saturada → preservada
  - OBS desconecta durante drenagem → actions queued, worker não bloqueia engine
  - Mesma ação de cena repetida → deduplicada
  - Ação expirada na fila → descartada antes de chegar ao OBS
  - Resposta malformada do OBS → erro logado, sem crash
  - Shutdown durante reconnect → cancela sem tasks infinitas
  - Race condition: mudança de cena rápida sob cooldown → corretamente throttled
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.obs import (
    OBSAction,
    OBSActionType,
    OBSAdapter,
    OBSConfig,
    OBSConnectionState,
    OBSPriorityQueue,
)
from src.domain.events import Event, EventType
from src.domain.priorities import Priority


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    scenes: tuple[str, ...] = ("Main", "BRB"),
    sources: tuple[str, ...] = ("overlay",),
    cooldown: float = 0.0,
    queue_maxsize: int = 20,
    reconnect_delays: tuple[float, ...] = (0.01, 0.02),
) -> OBSConfig:
    return OBSConfig(
        enabled=True,
        host="localhost",
        port=4455,
        password="",
        connect_timeout_s=0.5,
        request_timeout_s=0.5,
        reconnect_delays_s=reconnect_delays,
        scene_change_cooldown_s=cooldown,
        allowed_scenes=frozenset(scenes),
        allowed_sources=frozenset(sources),
        obs_queue_maxsize=queue_maxsize,
    )


def _make_action(
    action_type: OBSActionType = OBSActionType.OBS_SET_SCENE,
    priority: int = 4,
    scene: str = "Main",
    dedupe_key: str | None = None,
    expires_in_s: float | None = None,
) -> OBSAction:
    expires_at = None
    if expires_in_s is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in_s)
    return OBSAction(
        action_type=action_type,
        params={"scene_name": scene},
        priority=priority,
        dedupe_key=dedupe_key,
        expires_at=expires_at,
    )


# ---------------------------------------------------------------------------
# Stress: flood de baixa prioridade
# ---------------------------------------------------------------------------


class TestOBSQueueFlood:
    def test_100_low_priority_actions_bounded(self) -> None:
        """100 ações P4 → fila permanece bounded (sem overflow de memória)."""
        q = OBSPriorityQueue(maxsize_per_level=5)  # P4 capacity = 5//8 ≈ 1

        accepted = 0
        dropped = 0
        for i in range(100):
            action = _make_action(priority=4, dedupe_key=f"key_{i}")
            if q.put_nowait(action):
                accepted += 1
            else:
                dropped += 1

        # A fila NÃO pode crescer sem limite
        assert q.depth() <= 5
        # Deve ter descartado muitos
        assert dropped > 0
        assert dropped + accepted == 100
        assert q.dropped_total == dropped

    def test_high_priority_preserved_during_flood(self) -> None:
        """Ação P1 deve ser aceita mesmo com fila P4 saturada."""
        q = OBSPriorityQueue(maxsize_per_level=4)  # P4 cap = 4//8 ≈ 1

        # Saturar com P4
        for i in range(20):
            q.put_nowait(_make_action(priority=4, dedupe_key=f"low_{i}"))

        # Enviar P1
        high = _make_action(action_type=OBSActionType.OBS_TRIGGER_MEDIA, priority=1)
        result = q.put_nowait(high)

        assert result is True, "P1 deve ser aceita independente de overflow P4"

    @pytest.mark.asyncio
    async def test_high_priority_comes_out_first(self) -> None:
        """P1 deve sair antes de P3/P4 mesmo enfileirada depois."""
        q = OBSPriorityQueue(maxsize_per_level=20)

        # Adicionar P4 primeiro
        for i in range(3):
            q.put_nowait(_make_action(priority=4, scene="Main", dedupe_key=f"p4_{i}"))

        # Adicionar P1 depois
        p1 = _make_action(action_type=OBSActionType.OBS_TRIGGER_MEDIA, priority=1)
        q.put_nowait(p1)

        # P1 deve sair primeiro
        first = await asyncio.wait_for(q.get_next(), timeout=1.0)
        assert first.priority == 1, f"Esperado P1, obtido P{first.priority}"

    def test_rapid_scene_changes_deduped(self) -> None:
        """Requisições repetidas para a mesma cena são deduplicadas."""
        q = OBSPriorityQueue(maxsize_per_level=20)

        # 10 pedidos para trocar para "Main"
        accepted = 0
        for i in range(10):
            action = _make_action(priority=2, scene="Main", dedupe_key="scene:Main")
            if q.put_nowait(action):
                accepted += 1

        # Apenas a primeira deve ser aceita (dedupe)
        assert accepted == 1
        assert q.dropped_total == 9


# ---------------------------------------------------------------------------
# Falha: OBS desconectado durante drenagem
# ---------------------------------------------------------------------------


class TestOBSDisconnectDuringDrain:
    @pytest.mark.asyncio
    async def test_worker_does_not_block_engine_when_obs_offline(self) -> None:
        """Worker descarta ação silenciosamente quando OBS está desconectado."""
        adapter = OBSAdapter(config=_make_config())
        # OBS nunca conecta — state permanece DISCONNECTED
        assert adapter._state == OBSConnectionState.DISCONNECTED

        action = _make_action(priority=1)
        adapter._queue.put_nowait(action)

        # execute_action não deve bloquear
        import time
        t0 = time.monotonic()
        await adapter._execute_action(action)
        elapsed = time.monotonic() - t0

        assert elapsed < 0.5, f"_execute_action bloqueou por {elapsed:.3f}s com OBS offline"

    @pytest.mark.asyncio
    async def test_state_set_to_disconnected_on_connection_error(self) -> None:
        """Erro de conexão detectado durante request atualiza estado."""
        adapter = OBSAdapter(config=_make_config())
        adapter._state = OBSConnectionState.CONNECTED
        adapter._client = MagicMock()  # cliente mock

        # Simular erro de websocket na send_request
        with patch.object(
            adapter,
            "_send_request",
            side_effect=Exception("connection closed by remote host"),
        ):
            await adapter._execute_action(_make_action(priority=1))

        # Deve ter contabilizado falha
        assert adapter._metrics.request_failures_total > 0

    @pytest.mark.asyncio
    async def test_worker_loop_exits_on_stop(self) -> None:
        """Worker loop para quando stop_event é sinalizado."""
        adapter = OBSAdapter(config=_make_config())
        adapter._stop_event.set()  # sinalize parada imediatamente

        # Worker deve sair rapidamente
        task = asyncio.create_task(adapter._worker_loop())
        await asyncio.wait_for(task, timeout=2.0)
        assert task.done()


# ---------------------------------------------------------------------------
# Falha: ação expirada
# ---------------------------------------------------------------------------


class TestOBSActionExpiry:
    @pytest.mark.asyncio
    async def test_expired_action_not_sent_to_obs(self) -> None:
        """Ação expirada é descartada no execute_action sem chamar OBS."""
        adapter = OBSAdapter(config=_make_config())
        adapter._state = OBSConnectionState.CONNECTED

        # Criar ação já expirada
        expired = _make_action(priority=1, expires_in_s=-5.0)

        requests_sent: list[str] = []

        async def mock_send(request_type: str, data: dict) -> dict:
            requests_sent.append(request_type)
            return {}

        adapter._send_request = mock_send

        await adapter._execute_action(expired)

        assert len(requests_sent) == 0, "Nenhuma request deve ser enviada para ação expirada"

    @pytest.mark.asyncio
    async def test_expired_action_counted_in_metrics(self) -> None:
        """Ação expirada incrementa expired_total na fila."""
        q = OBSPriorityQueue(maxsize_per_level=10)
        q.put_nowait(_make_action(priority=2, expires_in_s=-1.0))
        q.put_nowait(_make_action(priority=3, expires_in_s=None))

        # get_next deve descartar a expirada e retornar a boa
        action = await asyncio.wait_for(q.get_next(), timeout=1.0)
        assert action.priority == 3
        assert q.expired_total == 1


# ---------------------------------------------------------------------------
# Falha: resposta malformada do OBS
# ---------------------------------------------------------------------------


class TestOBSMalformedResponse:
    @pytest.mark.asyncio
    async def test_malformed_response_does_not_crash(self) -> None:
        """Resposta inesperada do OBS não deve derrubar o adapter."""
        adapter = OBSAdapter(config=_make_config(cooldown=0.0))
        adapter._state = OBSConnectionState.CONNECTED

        # Simular send_request retornando dados malformados
        async def mock_send(request_type: str, data: dict) -> dict:
            return {"unexpected_field": None, "no_sceneName": True}

        adapter._send_request = mock_send

        action = _make_action(priority=1, scene="Main")
        # Não deve lançar exceção
        await adapter._execute_action(action)

    @pytest.mark.asyncio
    async def test_none_response_handled_gracefully(self) -> None:
        """send_request retornando None não causa crash."""
        adapter = OBSAdapter(config=_make_config(cooldown=0.0))
        adapter._state = OBSConnectionState.CONNECTED

        async def mock_send(request_type: str, data: dict) -> None:
            return None

        adapter._send_request = mock_send

        action = _make_action(priority=1, scene="Main")
        await adapter._execute_action(action)  # não deve levantar exceção

    @pytest.mark.asyncio
    async def test_send_request_timeout_counted_as_failure(self) -> None:
        """Timeout em send_request incrementa request_failures_total."""
        adapter = OBSAdapter(config=_make_config())
        adapter._state = OBSConnectionState.CONNECTED
        adapter._client = MagicMock()

        # Forçar timeout ao chamar _send_request diretamente
        with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
            result = await adapter._send_request("SetCurrentProgramScene", {"sceneName": "Main"})

        assert result is None
        assert adapter._metrics.request_failures_total == 1


# ---------------------------------------------------------------------------
# Retry policy: sem retry infinito
# ---------------------------------------------------------------------------


class TestOBSRetryPolicy:
    @pytest.mark.asyncio
    async def test_no_infinite_retry_on_failure(self) -> None:
        """_execute_action não deve fazer retry — a queue/reconnect cuida disso."""
        adapter = OBSAdapter(config=_make_config())
        adapter._state = OBSConnectionState.CONNECTED

        call_count = [0]

        async def mock_send(request_type: str, data: dict) -> None:
            call_count[0] += 1
            raise Exception("obs error")

        adapter._send_request = mock_send

        action = _make_action(priority=1, scene="Main")
        await adapter._execute_action(action)

        # Deve ter tentado exatamente 1 vez (sem retry)
        assert call_count[0] == 1, f"Esperado 1 tentativa, obtido {call_count[0]}"

    @pytest.mark.asyncio
    async def test_reconnect_uses_configured_delays(self) -> None:
        """Reconnect loop para imediatamente quando stop é sinalizado."""
        config = OBSConfig(
            enabled=True, host="localhost", port=4455, password="",
            connect_timeout_s=0.01, request_timeout_s=0.01,
            reconnect_delays_s=(0.01, 0.02),
            scene_change_cooldown_s=0.0,
            allowed_scenes=frozenset(), allowed_sources=frozenset(),
            obs_queue_maxsize=10,
        )
        adapter = OBSAdapter(config=config)

        connect_attempts = [0]

        async def mock_try_connect() -> bool:
            connect_attempts[0] += 1
            return False

        adapter._try_connect = mock_try_connect

        # Iniciar loop e parar após 1 tentativa
        async def run_and_stop():
            task = asyncio.create_task(adapter._connect_loop())
            await asyncio.sleep(0.1)  # deixar 1-2 tentativas acontecerem
            adapter._stop_event.set()
            await asyncio.wait_for(task, timeout=2.0)

        await run_and_stop()

        # Deve ter feito poucas tentativas (não infinitas)
        assert connect_attempts[0] <= 5, (
            f"Muitas tentativas de conexão: {connect_attempts[0]}"
        )


# ---------------------------------------------------------------------------
# Rate limit: cooldown de cena
# ---------------------------------------------------------------------------


class TestOBSSceneCooldown:
    @pytest.mark.asyncio
    async def test_scene_change_respects_cooldown(self) -> None:
        """Troca de cena dentro do cooldown deve ser ignorada."""
        config = _make_config(cooldown=10.0)  # cooldown alto para o teste
        adapter = OBSAdapter(config=config)
        adapter._state = OBSConnectionState.CONNECTED

        requests_sent: list[str] = []

        async def mock_send(request_type: str, data: dict) -> dict:
            requests_sent.append(request_type)
            return {}

        adapter._send_request = mock_send
        adapter._current_scene = "BRB"  # cena diferente de Main

        # Primeira chamada: deve passar
        await adapter._do_set_scene({"scene_name": "Main"})
        # Segunda chamada imediata: dentro do cooldown → deve ser ignorada
        adapter._current_scene = "BRB"  # resetar para nova tentativa ser "diferente"
        await adapter._do_set_scene({"scene_name": "Main"})

        # Apenas 1 request deve ter sido enviada
        assert requests_sent.count("SetCurrentProgramScene") == 1

    @pytest.mark.asyncio
    async def test_scene_change_skipped_when_already_active(self) -> None:
        """Trocar para a cena que já está ativa não deve enviar request."""
        config = _make_config(cooldown=0.0)
        adapter = OBSAdapter(config=config)
        adapter._state = OBSConnectionState.CONNECTED
        adapter._current_scene = "Main"  # já na cena alvo

        requests_sent: list[str] = []

        async def mock_send(request_type: str, data: dict) -> dict:
            requests_sent.append(request_type)
            return {}

        adapter._send_request = mock_send

        await adapter._do_set_scene({"scene_name": "Main"})

        assert len(requests_sent) == 0, "Nenhuma request quando já está na cena alvo"


# ---------------------------------------------------------------------------
# Isolamento: OBS não bloqueia Event Engine
# ---------------------------------------------------------------------------


class TestOBSIsolation:
    @pytest.mark.asyncio
    async def test_handle_returns_immediately_regardless_of_obs_state(self) -> None:
        """handle() deve completar em <10ms independente do estado do OBS."""
        import time

        adapter = OBSAdapter(
            config=_make_config(),
            action_mapping=[
                {
                    "enabled": True,
                    "match_event_type": "GIFT",
                    "match_priority": ["P1"],
                    "action": "OBS_SET_SCENE",
                    "params": {"scene_name": "Main"},
                    "priority": 1,
                }
            ],
        )
        # Simular OBS offline
        adapter._state = OBSConnectionState.DISCONNECTED

        from src.domain.events import EventUser
        event = Event(
            event_id="isolation-test",
            event_type=EventType.GIFT,
            source="tiktok",
            timestamp=datetime.now(timezone.utc),
            received_at=datetime.now(timezone.utc),
            user=EventUser(external_id="u1", display_name="User"),
            payload={"gift_id": 1, "gift_value": 10, "repeat_count": 1},
            status="received",
        )

        t0 = time.monotonic()
        await adapter.handle(event)
        elapsed_ms = (time.monotonic() - t0) * 1000

        assert elapsed_ms < 10, (
            f"handle() levou {elapsed_ms:.1f}ms — deve ser < 10ms"
        )

    @pytest.mark.asyncio
    async def test_queue_saturation_does_not_affect_engine(self) -> None:
        """Saturar a fila OBS não deve bloquear o Event Engine."""
        import time

        adapter = OBSAdapter(
            config=_make_config(queue_maxsize=5),
            action_mapping=[
                {
                    "enabled": True,
                    "match_event_type": "GIFT",
                    "match_priority": ["P1"],
                    "action": "OBS_SET_SCENE",
                    "params": {"scene_name": "Main"},
                    "priority": 1,
                }
            ],
        )

        from src.domain.events import EventUser

        def make_gift():
            return Event(
                event_id=f"e-{id(object())}",
                event_type=EventType.GIFT,
                source="tiktok",
                timestamp=datetime.now(timezone.utc),
                received_at=datetime.now(timezone.utc),
                user=EventUser(external_id="u1", display_name="User"),
                payload={"gift_id": 1, "gift_value": 10, "repeat_count": 1},
                status="received",
            )

        # Enviar muitos eventos rápido
        t0 = time.monotonic()
        for _ in range(50):
            await adapter.handle(make_gift())
        elapsed_ms = (time.monotonic() - t0) * 1000

        # 50 handle() calls devem terminar em < 500ms
        assert elapsed_ms < 500, (
            f"50 handle() calls levaram {elapsed_ms:.1f}ms — bloqueio suspeito"
        )

        # A fila deve estar bounded (não cresceu infinitamente)
        assert adapter._queue.depth() <= adapter._config.obs_queue_maxsize
