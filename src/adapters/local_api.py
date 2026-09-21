"""Local API — a fronteira HTTP que o Roblox (via HttpService) consome.

Framework: FastAPI + Uvicorn (ver DECISIONS.md — decisão que estava em
aberto desde a fase 1, resolvida agora porque o Event Engine é asyncio e
uma API sync (Flask puro) exigiria rodar num thread separado ou bloquear
o loop; FastAPI roda no mesmo loop, e a validação de request via Pydantic
cobre o `payload_validation` exigido pela fase 4 sem reinventar isso à mão).

Endpoints (contrato mínimo da fase 4, `api_contract`):
- GET  /health          -> disponibilidade do bridge, sem payload de evento
- GET  /events          -> eventos disponíveis pro Roblox (cursor-based)
- POST /ack             -> confirmação de progresso (observabilidade)

Este módulo NÃO decide gameplay, NÃO conhece TikTok e NÃO conhece Luau —
ele só expõe o que `RobloxBridge` já traduziu.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from src.adapters.roblox import SCHEMA_VERSION, RobloxBridge
from src.logging import get_logger

LOGGER = get_logger(__name__)

# Limites de request — nenhum endpoint aceita valores arbitrários
# (ver `local_api_security` da fase 1 e `payload_validation` da fase 4).
_MAX_LIMIT = 200
_MIN_LIMIT = 1


from src.observability.metrics import OperationalSnapshot


class AckRequest(BaseModel):
    """Corpo de `POST /ack`."""

    up_to_sequence: int = Field(ge=0)


def create_app(
    bridge: RobloxBridge, snapshot: OperationalSnapshot | None = None
) -> FastAPI:
    """Monta a aplicação FastAPI em torno de um `RobloxBridge` já existente."""
    app = FastAPI(
        title="tiktoklive local bridge api",
        version=SCHEMA_VERSION,
        # Desliga os docs automáticos por padrão: esta API não é pra ser
        # descoberta/navegada, é um contrato fixo pro Roblox consumir.
        docs_url=None,
        redoc_url=None,
    )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        """Disponibilidade do bridge. Expõe status consolidado se houver snapshot."""
        if snapshot:
            snap = snapshot.get_snapshot()
            status = snap["status"]
            return {
                "status": status,
                "bridge": bridge.health_snapshot(),
                "system": snap,
            }

        return {"status": "ok", "bridge": bridge.health_snapshot()}

    @app.get("/events")
    async def get_events(
        since: int = Query(default=0, ge=0),
        limit: int = Query(default=25, ge=_MIN_LIMIT, le=_MAX_LIMIT),
    ) -> dict[str, Any]:
        """Eventos disponíveis pro Roblox, a partir do cursor `since`.

        Não remove nada do buffer — ver `RobloxBridge.get_events_since`.
        `gap_detected=true` significa que eventos entre `since` e o mais
        antigo disponível foram descartados por eviction (buffer cheio)
        antes do Roblox consumi-los.
        """
        envelopes, cursor, gap_detected = bridge.get_events_since(since=since, limit=limit)
        return {
            "schema_version": SCHEMA_VERSION,
            "events": [e.to_dict() for e in envelopes],
            "cursor": cursor,
            "gap_detected": gap_detected,
        }

    @app.post("/ack")
    async def ack(request: AckRequest) -> dict[str, Any]:
        """Registra até onde o Roblox confirma ter processado (observabilidade)."""
        acknowledged = bridge.ack(up_to_sequence=request.up_to_sequence)
        return {"acknowledged_up_to": acknowledged}

    @app.exception_handler(ValueError)
    async def _value_error_handler(_request: Any, exc: ValueError) -> None:
        # Nunca deixa uma ValueError interna virar 500 opaco sem log.
        LOGGER.error("Local API: ValueError não tratada: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc))

    return app


def run(bridge: RobloxBridge | None = None, host: str = "127.0.0.1", port: int = 8787) -> None:
    """Sobe a Local API sozinha, pra teste manual sem o pipeline inteiro.

    Isto NÃO é o entrypoint de produção do sistema (esse ainda não existe —
    ver DECISIONS.md, "EM ABERTO — app.py de composição"). É só o suficiente
    pra alguém rodar `python -m src.adapters.local_api` e testar o polling
    do Roblox contra um bridge vazio ou alimentado manualmente num REPL.
    """
    import uvicorn

    app = create_app(bridge or RobloxBridge())
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run()
