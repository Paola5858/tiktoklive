"""Configuração da aplicação — centralizada, validada, sem segredos hardcoded.

Fase 1 só precisa do essencial: ambiente e nível de log. Configurações
novas (host/porta da API, limites de fila, timeouts) entram quando a
camada que precisa delas for implementada de verdade — não antes
(ver `configuration.rule` na spec da fase 1: "não criar dezenas de
configurações prematuramente").
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from src.errors import ConfigurationError

_VALID_ENVIRONMENTS = {"local", "development", "staging", "production"}
_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_DEFAULT_ENVIRONMENT = "local"
_DEFAULT_LOG_LEVEL = "INFO"


@dataclass(frozen=True, slots=True)
class Settings:
    """Configuração validada da aplicação."""

    environment: str
    log_level: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        """Monta as configurações a partir de variáveis de ambiente.

        `env` é injetável pra permitir teste sem depender do `os.environ`
        real do processo.
        """
        source = env if env is not None else os.environ

        environment = source.get("ENVIRONMENT", _DEFAULT_ENVIRONMENT).strip().lower()
        if environment not in _VALID_ENVIRONMENTS:
            raise ConfigurationError(
                f"ENVIRONMENT inválido: {environment!r}. "
                f"Esperado um de: {sorted(_VALID_ENVIRONMENTS)}"
            )

        log_level = source.get("LOG_LEVEL", _DEFAULT_LOG_LEVEL).strip().upper()
        if log_level not in _VALID_LOG_LEVELS:
            raise ConfigurationError(
                f"LOG_LEVEL inválido: {log_level!r}. "
                f"Esperado um de: {sorted(_VALID_LOG_LEVELS)}"
            )

        return cls(environment=environment, log_level=log_level)
