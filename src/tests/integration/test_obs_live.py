"""Teste de integração E2E contra OBS Studio REAL rodando na máquina.

Verifica:
  1. Conexão WebSocket 5.x no porta 4455 sem auth.
  2. Health Snapshot registrando estado CONNECTED e latência real.
  3. Troca de cena para 'BRB' e retorno para 'Main'.
  4. Atualização do texto da fonte 'event_ticker'.
  5. Desconexão graciosa.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

import pytest

from src.adapters.obs import (
    OBSActionType,
    OBSAdapter,
    OBSConfig,
    OBSConnectionState,
)


@pytest.mark.asyncio
async def test_real_obs_integration() -> None:
    """Validação contra OBS Studio REAL em execução na máquina."""
    config = OBSConfig(
        enabled=True,
        host="localhost",
        port=4455,
        password="",
        connect_timeout_s=5.0,
        request_timeout_s=5.0,
        reconnect_delays_s=(1.0, 2.0),
        scene_change_cooldown_s=0.0,
        allowed_scenes=frozenset({"Main", "BRB", "Starting"}),
        allowed_sources=frozenset({"event_ticker"}),
        obs_queue_maxsize=10,
    )

    action_mapping = [
        {
            "enabled": True,
            "match_event_type": "GIFT",
            "match_priority": ["P1"],
            "action": "OBS_SET_SCENE",
            "params": {"scene_name": "BRB"},
            "priority": 1,
        }
    ]

    adapter = OBSAdapter(config=config, action_mapping=action_mapping)
    await adapter.start()

    try:
        # Aguardar conexão WebSocket
        for _ in range(30):
            if adapter._state == OBSConnectionState.CONNECTED:
                break
            await asyncio.sleep(0.2)

        if adapter._state != OBSConnectionState.CONNECTED:
            pytest.skip(
                f"OBS Studio não está em execução na máquina local (porta {config.port}). "
                "Pulando teste de integração real."
            )

        # Health snapshot
        snap = adapter.health_snapshot()
        assert snap["connected"] is True
        assert snap["status"] == "healthy"
        print(f"\n[OBS REAL] Conectado com sucesso! Snapshot: {snap}")

        # Teste 1: Atualizar texto do ticker
        res_text = await adapter._send_request(
            "SetInputSettings",
            {"inputName": "event_ticker", "inputSettings": {"text": "🔥 TikTok Live Real Test 🔥"}},
        )
        assert res_text is not None
        print("[OBS REAL] SetInputSettings (event_ticker) -> OK!")

        # Teste 2: Trocar de cena para BRB
        res_scene = await adapter._send_request(
            "SetCurrentProgramScene",
            {"sceneName": "BRB"},
        )
        assert res_scene is not None
        print("[OBS REAL] SetCurrentProgramScene (BRB) -> OK!")

        await asyncio.sleep(1.0)

        # Teste 3: Verificar cena atual
        res_curr = await adapter._send_request("GetCurrentProgramScene", {})
        assert res_curr is not None
        current_scene_name = res_curr.get("current_program_scene_name") or res_curr.get("currentProgramSceneName")
        print(f"[OBS REAL] GetCurrentProgramScene -> {current_scene_name}")
        assert current_scene_name == "BRB"

        # Teste 4: Voltar para cena Main
        res_main = await adapter._send_request(
            "SetCurrentProgramScene",
            {"sceneName": "Main"},
        )
        assert res_main is not None
        print("[OBS REAL] SetCurrentProgramScene (Main) -> OK!")

    finally:
        await adapter.stop()
        assert adapter._state in (
            OBSConnectionState.DISCONNECTED,
            OBSConnectionState.STOPPED,
        )
        print("[OBS REAL] Desconectado com sucesso.")


if __name__ == "__main__":
    asyncio.run(test_real_obs_integration())
