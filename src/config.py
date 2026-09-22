"""Configuração central do Live Engine.

A borda converte strings do ambiente em valores tipados. O restante da
aplicação recebe configuração já validada e não espalha ``os.environ`` pelo
código.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from src.errors import ConfigurationError

_VALID_ENVIRONMENTS = {"local", "development", "staging", "production"}
_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_VALID_MODES = {"live", "simulation", "replay"}


def _raw(source: Mapping[str, str], key: str, default: str) -> str:
    return str(source.get(key, default)).strip()


def parse_bool(source: Mapping[str, str], key: str, default: bool = False) -> bool:
    raw = _raw(source, key, "true" if default else "false").lower()
    if raw not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
        raise ConfigurationError(f"{key} inválido: use true/false")
    return raw in {"1", "true", "yes", "on"}


def parse_int(source: Mapping[str, str], key: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        value = int(_raw(source, key, str(default)))
    except ValueError as exc:
        raise ConfigurationError(f"{key} deve ser um inteiro") from exc
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{key} deve ser >= {minimum}; recebido {value}")
    if maximum is not None and value > maximum:
        raise ConfigurationError(f"{key} deve ser <= {maximum}; recebido {value}")
    return value


def parse_float(source: Mapping[str, str], key: str, default: float, *, minimum: float | None = None, maximum: float | None = None) -> float:
    try:
        value = float(_raw(source, key, str(default)))
    except ValueError as exc:
        raise ConfigurationError(f"{key} deve ser um número") from exc
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{key} deve ser >= {minimum}; recebido {value}")
    if maximum is not None and value > maximum:
        raise ConfigurationError(f"{key} deve ser <= {maximum}; recebido {value}")
    return value


def parse_csv(source: Mapping[str, str], key: str, default: str = "") -> tuple[str, ...]:
    return tuple(item.strip() for item in _raw(source, key, default).split(",") if item.strip())


def parse_float_csv(source: Mapping[str, str], key: str, default: str) -> tuple[float, ...]:
    values = parse_csv(source, key, default)
    if not values:
        raise ConfigurationError(f"{key} não pode ficar vazio")
    try:
        parsed = tuple(float(value) for value in values)
    except ValueError as exc:
        raise ConfigurationError(f"{key} deve ser uma lista de números separados por vírgula") from exc
    if any(value <= 0 for value in parsed):
        raise ConfigurationError(f"{key} deve conter somente números positivos")
    return parsed


@dataclass(frozen=True, slots=True)
class FeatureFlags:
    """Integrações que podem ser ligadas sem tornar o core dependente delas."""

    tiktok: bool = True
    roblox: bool = True
    obs: bool = False
    mqtt: bool = False
    event_recording: bool = True
    debug_logging: bool = False

    @classmethod
    def from_env(cls, source: Mapping[str, str], *, mode: str) -> "FeatureFlags":
        return cls(
            tiktok=parse_bool(source, "FEATURE_TIKTOK", mode == "live"),
            roblox=parse_bool(source, "FEATURE_ROBLOX", True),
            obs=parse_bool(source, "FEATURE_OBS", False),
            mqtt=parse_bool(source, "FEATURE_MQTT", False),
            event_recording=parse_bool(source, "FEATURE_EVENT_RECORDING", True),
            debug_logging=parse_bool(source, "FEATURE_DEBUG_LOGGING", False),
        )

    def as_dict(self) -> dict[str, bool]:
        return {
            "tiktok": self.tiktok,
            "roblox": self.roblox,
            "obs": self.obs,
            "mqtt": self.mqtt,
            "event_recording": self.event_recording,
            "debug_logging": self.debug_logging,
        }


@dataclass(frozen=True, slots=True)
class Settings:
    """Configuração básica validada e flags efetivas da execução."""

    environment: str
    log_level: str
    mode: str = "live"
    features: FeatureFlags = FeatureFlags()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        source = env if env is not None else os.environ
        environment = _raw(source, "ENVIRONMENT", "local").lower()
        if environment not in _VALID_ENVIRONMENTS:
            raise ConfigurationError(f"ENVIRONMENT inválido: {environment!r}. Esperado: {sorted(_VALID_ENVIRONMENTS)}")
        log_level = _raw(source, "LOG_LEVEL", "INFO").upper()
        if log_level not in _VALID_LOG_LEVELS:
            raise ConfigurationError(f"LOG_LEVEL inválido: {log_level!r}. Esperado: {sorted(_VALID_LOG_LEVELS)}")
        mode = _raw(source, "RUN_MODE", "live").lower()
        if mode not in _VALID_MODES:
            raise ConfigurationError(f"RUN_MODE inválido: {mode!r}. Esperado: {sorted(_VALID_MODES)}")
        return cls(environment, log_level, mode, FeatureFlags.from_env(source, mode=mode))


__all__ = ["ConfigurationError", "FeatureFlags", "Settings", "parse_bool", "parse_csv", "parse_float", "parse_float_csv", "parse_int"]
