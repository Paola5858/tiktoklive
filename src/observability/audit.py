"""Auditoria de eventos.

Registra a trilha de eventos processados em JSONL para análise
e debugging, garantindo que não consuma disco indefinidamente
nem bloqueie o event loop principal.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.logging import get_logger

LOGGER = get_logger(__name__)


class EventAuditLogger:
    """Grava logs de auditoria em arquivo JSONL.

    Usa uma fila assíncrona para não bloquear o event loop durante a escrita no disco.
    A rotação de arquivos é feita criando um arquivo novo por dia (YYYY-MM-DD.jsonl).
    """

    def __init__(self, log_dir: str = "events", max_queue_size: int = 5000):
        self.log_dir = Path(log_dir)
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=max_queue_size)
        self._writer_task: asyncio.Task[None] | None = None
        self._shutdown = asyncio.Event()

    async def start(self) -> None:
        """Inicia a task de escrita em disco."""
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            LOGGER.error("Falha ao criar diretório de audit log: %s", e)
            return

        self._shutdown.clear()
        self._writer_task = asyncio.create_task(self._write_loop(), name="audit-writer")

    async def stop(self) -> None:
        """Encerra a task de escrita, aguardando o esvaziamento da fila."""
        self._shutdown.set()
        if self._writer_task:
            await self._writer_task

    def log_event(self, event_data: dict[str, Any]) -> None:
        """Enfileira um evento para auditoria. (Non-blocking)"""
        if self._shutdown.is_set():
            return

        try:
            line = json.dumps(event_data, default=str) + "\n"
            # Usa put_nowait para nunca bloquear o chamador
            self._queue.put_nowait(line)
        except asyncio.QueueFull:
            # Fila cheia: descarta log de auditoria para não derrubar o sistema
            LOGGER.warning("Audit queue full, descartando log de evento")
        except Exception as e:
            LOGGER.error("Falha ao serializar evento para audit: %s", e)

    async def _write_loop(self) -> None:
        """Loop de escrita em background."""
        current_date = ""
        current_file = None

        try:
            while not self._shutdown.is_set() or not self._queue.empty():
                try:
                    # Timeout para permitir verificação do shutdown
                    line = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                # Rotação diária simples baseada em UTC
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                if today != current_date:
                    if current_file:
                        current_file.close()

                    current_date = today
                    file_path = self.log_dir / f"{today}.jsonl"

                    try:
                        # Append mode
                        current_file = open(file_path, "a", encoding="utf-8")
                    except OSError as e:
                        LOGGER.error("Falha ao abrir arquivo de audit %s: %s", file_path, e)
                        current_file = None

                if current_file:
                    try:
                        # Escrita síncrona (geralmente rápida graças ao buffer do SO),
                        # mas executada fora do loop crítico (esta task pode atrasar, mas o put_nowait não bloqueia).
                        current_file.write(line)
                        current_file.flush()
                    except OSError as e:
                        LOGGER.error("Falha ao escrever no audit log: %s", e)

                self._queue.task_done()

        except asyncio.CancelledError:
            pass
        finally:
            if current_file:
                current_file.close()
