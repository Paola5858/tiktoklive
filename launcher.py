"""Launcher automático do TikTok x Roblox Live Engine.

Abre, na ordem:
  1. Navegador web apontando para a live do TikTok (@live.engine)
  2. OBS Studio (com obs-websocket em 4455)
  3. Roblox Studio (com os ModuleScripts em ServerScriptService)
  4. Engine Python (Local API :8787 + TikTok connector + OBS adapter)

Nenhum clique manual é necessário — o launcher dispara tudo e o engine
fica em segundo plano. Use o `stop_all.bat` ou feche este script para parar.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TIKTOK_USER = "live.engine"
TIKTOK_URL = f"https://www.tiktok.com/@{TIKTOK_USER}/live"


def _find_executable(names: list[str], extra_dirs: list[str] | None = None) -> str | None:
    """Localiza um executável pelo nome em PATH ou em caminhos comuns."""
    for name in names:
        found = shutil.which(name)
        if found:
            return found

    # Caminhos comuns do Windows — também varre subdiretórios de instalação
    # conhecidos (ex.: Roblox versions/) para capturar nomes de arquivo
    # que mudam entre versões (obs.exe vs obs64.exe, StudioInstaller vs Launcher).
    candidates: list[str] = [
        "C:\\Program Files\\obs-studio\\bin\\64bit\\obs.exe",
        "C:\\Program Files\\obs-studio\\bin\\64bit\\obs64.exe",
        "C:\\Program Files (x86)\\obs-studio\\bin\\64bit\\obs.exe",
        "C:\\Program Files (x86)\\obs-studio\\bin\\64bit\\obs64.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "obs-studio", "bin", "64bit", "obs.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "obs-studio", "bin", "64bit", "obs64.exe"),
    ]
    for path in candidates:
        if Path(path).is_file():
            return path

    # Busca em diretórios extra (ex.: versões do Roblox)
    if extra_dirs:
        for base in extra_dirs:
            if not Path(base).is_dir():
                continue
            for name in names:
                # Busca exata no diretório base
                candidate = Path(base) / name
                if candidate.is_file():
                    return str(candidate)
            # Busca recursiva por nomes que contenham "Studio" ou "obs"
            for name in names:
                pattern = f"**/{name}"
                for match in Path(base).glob(pattern):
                    if match.is_file():
                        return str(match)
    return None


def _open_url(url: str) -> None:
    """Abre a URL no navegador padrão."""
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass


def _start_process(cmd: str | list[str], wait: float = 0.0) -> subprocess.Popen | None:
    """Inicia um processo em segundo plano."""
    try:
        proc = subprocess.Popen(cmd, shell=isinstance(cmd, str))
        if wait:
            time.sleep(wait)
        return proc
    except Exception as exc:
        print(f"[launcher] Aviso: nao foi possível iniciar {cmd}: {exc}")
        return None


def main() -> None:
    print("=" * 60)
    print("  TikTok x Roblox Live Engine — Launcher")
    print(f"  TikTok: @{TIKTOK_USER}")
    print("=" * 60)
    print()

    # 1. Abrir a live do TikTok no navegador
    print(f"[1/4] Abrindo live do TikTok: {TIKTOK_URL}")
    _open_url(TIKTOK_URL)
    time.sleep(2)

    # 2. Abrir OBS Studio
    print("[2/4] Iniciando OBS Studio...")
    obs_path = _find_executable(["obs.exe", "obs64.exe"])
    if obs_path:
        print(f"      OBS encontrado: {obs_path}")
        _start_process([obs_path], wait=3)
    else:
        print("      [aviso] OBS Studio nao encontrado em caminhos padrao.")
        print("      Abra manualmente e habilite o obs-websocket (porta 4455).")

    # 3. Abrir Roblox Studio
    print("[3/4] Iniciando Roblox Studio...")
    roblox_versions = os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "Roblox", "Versions"
    )
    roblox_path = _find_executable(
        ["RobloxStudioLauncher.exe", "RobloxStudioInstaller.exe", "RobloxStudioBeta.exe"],
        extra_dirs=[roblox_versions],
    )
    if roblox_path:
        print(f"      Roblox Studio encontrado: {roblox_path}")
        _start_process([roblox_path], wait=3)
    else:
        print("      [aviso] Roblox Studio nao encontrado.")
        print("      Abra manualmente e cole os ModuleScripts de roblox/src em ServerScriptService.")

    # 4. Iniciar o Engine Python (em segundo plano, sem janela)
    print("[4/4] Iniciando Live Engine (Python + Local API :8787 + TikTok connector)...")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    proc = _start_process(
        [sys.executable, "-m", "src.app"],
        wait=0.0,
    )
    if proc:
        print(f"      Engine Python iniciado (PID {proc.pid})")
    else:
        print("      [erro] Falha ao iniciar o engine Python.")

    print()
    print("[launcher] Tudo iniciado!")
    print("[launcher] Engine Python rodando em segundo plano.")
    print("[launcher] Use o stop_all.bat para parar todos os componentes.")
    print("[launcher] Feche este prompt com X para manter os processos.")
    print()

    # Mantém o launcher vivo
    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        print("\n[launcher] Encerrando...")


if __name__ == "__main__":
    main()