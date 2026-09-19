"""Erros de domínio.

Erros de domínio representam violação de invariantes do próprio domínio
(um `Event` ou `Command` construído de forma inválida) — não erros de
infraestrutura, rede ou configuração, que vivem em `src/errors.py`.
"""

from __future__ import annotations

from src.errors import AppError


class DomainError(AppError):
    """Erro base pra qualquer violação de invariante do domínio."""


class InvalidEventError(DomainError):
    """Um `Event` foi construído violando uma invariante do schema interno."""


class InvalidCommandError(DomainError):
    """Um `Command` foi construído violando uma invariante do schema interno."""


class UnknownEventTypeError(DomainError):
    """Um tipo de evento não reconhecido chegou no domínio."""
