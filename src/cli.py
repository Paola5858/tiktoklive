"""Interface oficial de operações locais do Live Engine."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Sequence

from src.config import ConfigurationError
from src.domain.events import Event, EventType, EventUser
from src.interaction import InteractionRuleEngine, load_rules_file

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PID_FILE = ROOT / "logs/liveengine.pid"


def _pid_path() -> Path:
    return Path(os.environ.get("PID_FILE", str(DEFAULT_PID_FILE)))


def _read_pid() -> int | None:
    try:
        value = int(_pid_path().read_text(encoding="utf-8").strip())
        if value <= 0:
            return None
        return value
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        # No signal 0 confiável em todos os ambientes Windows; o pidfile é a
        # única evidência disponível e o status ficará explicitamente incerto.
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _health_url() -> str:
    host = os.environ.get("LOCAL_API_HOST", "127.0.0.1")
    port = os.environ.get("LOCAL_API_PORT", "8787")
    return f"http://{host}:{port}/health"


def _get_health(timeout: float = 1.5) -> tuple[bool, dict | str]:
    try:
        with urllib.request.urlopen(_health_url(), timeout=timeout) as response:
            return True, json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return False, str(exc)


def _load_config() -> AppConfig:
    from src.app import AppConfig, _load_env_file

    _load_env_file()
    return AppConfig.from_env()


def cmd_version(_args: argparse.Namespace) -> int:
    try:
        from importlib.metadata import version
        value = version("tiktoklive-engine")
    except Exception:
        value = "0.1.0"
    print(f"tiktoklive-engine {value}")
    return 0


def cmd_check(_args: argparse.Namespace) -> int:
    try:
        config = _load_config()
    except (ConfigurationError, ValueError) as exc:
        print(f"FAIL configuração: {exc}", file=sys.stderr)
        print("Dica: use RUN_MODE=simulation para validar o core sem TikTok.", file=sys.stderr)
        return 2

    checks: list[tuple[str, bool, str]] = []
    checks.append(("python", sys.version_info >= (3, 10), f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"))
    checks.append(("configuração", True, f"ambiente={config.settings.environment}, modo={config.settings.mode}"))
    checks.append(("regras", True, config.interaction_rules_path))
    checks.append(("Local API port", _port_available(config.local_api_host, config.local_api_port), f"{config.local_api_host}:{config.local_api_port}"))
    checks.append(("TikTok", bool(config.tiktok_unique_id) or not config.settings.features.tiktok, "configurado" if config.tiktok_unique_id else "desabilitado"))
    checks.append(("OBS", not config.settings.features.obs or not config.obs.enabled or bool(config.obs.allowed_scenes), "desabilitado ou allowlist presente"))
    checks.append(("MQTT", not config.settings.features.mqtt or not config.mqtt.enabled or bool(config.mqtt.allowed_devices), "desabilitado ou devices allowlisted"))
    for name, ok, detail in checks:
        print(f"{'OK' if ok else 'FAIL':4} {name}: {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 2


def _port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) != 0


def cmd_config_validate(_args: argparse.Namespace) -> int:
    return cmd_check(_args)


def cmd_status(_args: argparse.Namespace) -> int:
    pid = _read_pid()
    alive = pid is not None and _pid_alive(pid)
    api_ok, health = _get_health()
    if alive and api_ok:
        status = health.get("status", "unknown") if isinstance(health, dict) else "unknown"
        print(json.dumps({"process": "running", "pid": pid, "api": "ready", "health": status}, ensure_ascii=False))
        return 0
    details = {"process": "running" if alive else "stopped", "pid": pid, "api": "ready" if api_ok else "unavailable"}
    if not api_ok:
        details["api_detail"] = health
    print(json.dumps(details, ensure_ascii=False))
    return 0 if alive else 1


def cmd_start(_args: argparse.Namespace) -> int:
    from src.app import _load_env_file

    _load_env_file()
    pid = _read_pid()
    if pid and _pid_alive(pid):
        print(f"Live Engine já está rodando, PID {pid}.")
        return 1
    try:
        config = _load_config()
    except (ConfigurationError, ValueError) as exc:
        print(f"Não iniciado: configuração inválida: {exc}", file=sys.stderr)
        return 2
    log_path = ROOT / "logs" / "engine.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as stream:
        kwargs = {"cwd": str(ROOT), "env": os.environ.copy(), "stdout": stream, "stderr": stream}
        if os.name != "nt":
            kwargs["start_new_session"] = True
        import subprocess
        process = subprocess.Popen([sys.executable, "-m", "src.app"], **kwargs)
    print(f"Live Engine iniciado, PID {process.pid}. Logs: {log_path}")
    return 0


def cmd_stop(_args: argparse.Namespace) -> int:
    pid = _read_pid()
    if not pid or not _pid_alive(pid):
        print("Live Engine já está parado.")
        _pid_path().unlink(missing_ok=True)
        return 0
    if os.name == "nt":
        import subprocess
        subprocess.run(["taskkill", "/PID", str(pid), "/T"], check=False)
    else:
        os.kill(pid, signal.SIGTERM)
    print(f"Sinal de parada enviado ao PID {pid}.")
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    _load_env_file_without_runtime_dependencies()
    rules_path = Path(os.environ.get("INTERACTION_RULES_PATH", "configs/interaction_rules.json"))
    rules = load_rules_file(rules_path)
    engine = InteractionRuleEngine(rules)
    produced = 0
    for index in range(args.count):
        event_type = EventType.GIFT if index % 5 == 0 else EventType.COMMENT
        payload = {"gift_id": 101, "gift_name": "example_gift", "repeat_count": 1} if event_type == EventType.GIFT else {"text": "simulation roblox"}
        event = Event(event_type, "simulation", EventUser("simulated_viewer", f"simulation:{index % 3}"), payload, event_id=f"simulation-{index}")
        game_events = engine.evaluate(event)
        produced += len(game_events)
        if args.dry_run and game_events:
            for item in game_events:
                print(json.dumps(item.to_dict(), ensure_ascii=False))
        if args.interval > 0:
            time.sleep(args.interval)
    print(json.dumps({"mode": "simulation", "dry_run": args.dry_run, "input_events": args.count, "game_events": produced, "metrics": engine.metrics.snapshot()}, ensure_ascii=False))
    return 0


def _load_env_file_without_runtime_dependencies() -> None:
    """Carrega apenas o .env necessário à simulação, sem importar o app."""
    for path in (ROOT / ".env", Path.cwd() / ".env"):
        if not path.is_file():
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() and key.strip() not in os.environ:
                    os.environ[key.strip()] = value.strip().strip('"').strip("'")
        except OSError:
            pass
        break


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="liveengine", description="Operações locais do TikTok × Roblox Live Engine.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("version", help="exibe a versão instalada").set_defaults(func=cmd_version)
    sub.add_parser("check", help="valida ambiente, configuração, dependências e portas").set_defaults(func=cmd_check)
    sub.add_parser("status", help="mostra processo, Local API e health").set_defaults(func=cmd_status)
    sub.add_parser("start", help="inicia uma instância local em background").set_defaults(func=cmd_start)
    sub.add_parser("stop", help="envia shutdown gracioso para a instância local").set_defaults(func=cmd_stop)
    config = sub.add_parser("config", help="operações de configuração")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("validate", help="valida .env e arquivos de configuração").set_defaults(func=cmd_config_validate)
    simulate = sub.add_parser("simulate", help="gera eventos determinísticos sem TikTok, OBS, MQTT ou Roblox")
    simulate.add_argument("--count", type=int, default=10, help="quantidade de eventos sintéticos")
    simulate.add_argument("--interval", type=float, default=0.0, help="intervalo entre eventos, em segundos")
    simulate.add_argument("--dry-run", action="store_true", help="imprime GameEvents e não publica efeitos externos")
    simulate.set_defaults(func=cmd_simulate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
