"""Testes de fundação: `Settings`."""

from __future__ import annotations

import pytest

from src.config import Settings
from src.errors import ConfigurationError


def test_defaults_are_used_when_env_is_empty():
    settings = Settings.from_env(env={})

    assert settings.environment == "local"
    assert settings.log_level == "INFO"


def test_valid_env_vars_are_respected():
    settings = Settings.from_env(env={"ENVIRONMENT": "production", "LOG_LEVEL": "debug"})

    assert settings.environment == "production"
    assert settings.log_level == "DEBUG"


def test_invalid_environment_raises_configuration_error():
    with pytest.raises(ConfigurationError):
        Settings.from_env(env={"ENVIRONMENT": "not-a-real-env"})


def test_invalid_log_level_raises_configuration_error():
    with pytest.raises(ConfigurationError):
        Settings.from_env(env={"LOG_LEVEL": "SUPER_VERBOSE"})
