"""Comando abstrato — o contrato que sai do EVENT_ENGINE rumo à fila/bridge.

Um `Command` não carrega mais nenhum resquício da origem (TikTok, etc.).
Ver `context/EVENT_SCHEMA.md`, seção "evento pós EVENT_ENGINE".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from src.domain.errors import InvalidCommandError
from src.domain.priorities import Priority


class CommandType(str, Enum):
    """Comandos de jogo abstratos que o Roblox sabe executar."""

    SPAWN_AVATAR = "SPAWN_AVATAR"
    APPLY_EFFECT = "APPLY_EFFECT"
    REMOVE_ENTITY = "REMOVE_ENTITY"
    SYSTEM_SIGNAL = "SYSTEM_SIGNAL"


@dataclass(frozen=True, slots=True)
class Command:
    """Comando de jogo abstrato, pronto pra ser consumido pelo Roblox bridge.

    `roblox_user_id` é deliberadamente diferente do `external_id` de
    `EventUser` — a resolução de identidade TikTok → Roblox ainda é uma
    decisão em aberto (ver `context/DECISIONS.md`), então esse campo
    pode vir vazio até essa resolução existir de verdade.
    """

    command_type: CommandType
    priority: Priority
    params: dict[str, Any] = field(default_factory=dict)
    command_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    roblox_user_id: str | None = None
    issued_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not isinstance(self.params, dict):
            raise InvalidCommandError(
                f"Command.params precisa ser um dict, recebeu {type(self.params).__name__}"
            )
        if self.issued_at.tzinfo is None:
            raise InvalidCommandError("Command.issued_at precisa ser timezone-aware")
