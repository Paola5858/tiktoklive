from __future__ import annotations

import json
from pathlib import Path

from src.app import AppConfig
from src.cli import build_parser, cmd_simulate
from src.config import ConfigurationError, Settings


def test_simulation_defaults_disable_live_only_dependency():
    settings = Settings.from_env({"RUN_MODE": "simulation"})
    assert settings.mode == "simulation"
    assert settings.features.tiktok is False
    assert settings.features.obs is False
    assert settings.features.mqtt is False


def test_app_config_simulation_does_not_require_tiktok_id():
    config = AppConfig.from_env({"RUN_MODE": "simulation", "FEATURE_TIKTOK": "false"})
    assert config.tiktok_unique_id == ""
    assert config.local_api_port == 8787


def test_app_config_rejects_invalid_port_with_actionable_error():
    try:
        AppConfig.from_env({"RUN_MODE": "simulation", "LOCAL_API_PORT": "99999"})
    except ConfigurationError as exc:
        assert "LOCAL_API_PORT" in str(exc)
    else:
        raise AssertionError("porta inválida deveria falhar antes do startup")


def test_app_config_rejects_invalid_feature_flag():
    try:
        Settings.from_env({"FEATURE_OBS": "maybe"})
    except ConfigurationError as exc:
        assert "FEATURE_OBS" in str(exc)
    else:
        raise AssertionError("feature flag inválida deveria falhar")


def test_app_config_rejects_invalid_backoff_list():
    try:
        AppConfig.from_env({"RUN_MODE": "simulation", "TIKTOK_BACKOFF_DELAYS": "2,nope"})
    except ConfigurationError as exc:
        assert "TIKTOK_BACKOFF_DELAYS" in str(exc)
    else:
        raise AssertionError("backoff inválido deveria falhar")


def test_cli_exposes_expected_commands():
    parser = build_parser()
    for command in ("version", "check", "status", "start", "stop", "simulate"):
        args = parser.parse_args([command])
        assert args.command == command
    assert parser.parse_args(["config", "validate"]).config_command == "validate"


def test_simulation_dry_run_emits_game_events(capsys, monkeypatch):
    monkeypatch.setenv("INTERACTION_RULES_PATH", str(Path("configs/interaction_rules.json")))
    exit_code = cmd_simulate(type("Args", (), {"count": 5, "interval": 0.0, "dry_run": True})())
    assert exit_code == 0
    output = capsys.readouterr().out.strip().splitlines()
    assert any(json.loads(line).get("event_type") == "GAME_COMMAND" for line in output[:-1])
    summary = json.loads(output[-1])
    assert summary["mode"] == "simulation"
    assert summary["input_events"] == 5
