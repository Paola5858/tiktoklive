"""Testes de fundação do domínio: `Command`."""

from __future__ import annotations

import pytest

from src.domain.commands import Command, CommandType
from src.domain.errors import InvalidCommandError
from src.domain.priorities import Priority


def test_command_creation_with_minimum_fields_succeeds():
    command = Command(command_type=CommandType.SPAWN_AVATAR, priority=Priority.P1)

    assert command.command_id
    assert command.issued_at.tzinfo is not None
    assert command.params == {}


def test_command_with_invalid_params_type_raises():
    with pytest.raises(InvalidCommandError):
        Command(command_type=CommandType.SPAWN_AVATAR, priority=Priority.P1, params="não é dict")  # type: ignore[arg-type]


def test_two_commands_get_different_ids():
    a = Command(command_type=CommandType.APPLY_EFFECT, priority=Priority.P2)
    b = Command(command_type=CommandType.APPLY_EFFECT, priority=Priority.P2)

    assert a.command_id != b.command_id


def test_command_without_roblox_user_id_is_allowed():
    # a resolução de identidade TikTok -> Roblox ainda não existe (ver
    # DECISIONS.md), então um Command precisa conseguir existir sem ela.
    command = Command(command_type=CommandType.SYSTEM_SIGNAL, priority=Priority.P0)

    assert command.roblox_user_id is None
