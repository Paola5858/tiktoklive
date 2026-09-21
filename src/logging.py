"""Configuração de logging estruturado.

Objetivo: observabilidade desde o início.
Logs de console são legíveis para desenvolvimento.
Logs em arquivo são JSON estruturado com retenção diária.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Formato console amigável
_CONSOLE_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


class JsonFormatter(logging.Formatter):
    """Formatador de log estruturado em JSON para arquivos.

    Campos mínimos garantidos: timestamp, level, component, message.
    Campos opcionais incluídos se presentes no extra={...}:
    event, event_id, source_event_id, action_id, duration_ms, error_type, retry_count.
    """

    def format(self, record: logging.LogRecord) -> str:
        # Obter a mensagem baseada nos args
        if record.args:
            try:
                message = record.msg % record.args
            except TypeError:
                message = record.msg
        else:
            message = record.msg

        log_obj: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat() + "Z",
            "level": record.levelname,
            "component": record.name,
            "message": str(message),
        }

        # Adicionar exception se houver
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)

        # Campos opcionais estruturados (se passados via `extra`)
        for field in [
            "event",
            "event_id",
            "source_event_id",
            "action_id",
            "duration_ms",
            "error_type",
            "retry_count",
        ]:
            if hasattr(record, field):
                log_obj[field] = getattr(record, field)

        return json.dumps(log_obj)


def setup_logging(level: str, log_dir: str = "logs") -> None:
    """Configura o logging raiz da aplicação (console + file).

    Chamar uma única vez, na inicialização do app.
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Remover handlers antigos se reconfigurando
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # 1. Console Handler (texto)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(_CONSOLE_FORMAT))
    root_logger.addHandler(console_handler)

    # 2. File Handler (JSON, com rotação diária e max backup)
    # Rotação diária à meia-noite, mantendo 7 dias.
    path = Path(log_dir)
    try:
        path.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.TimedRotatingFileHandler(
            filename=path / "engine.log",
            when="midnight",
            interval=1,
            backupCount=7,
            encoding="utf-8",
        )
        file_handler.setFormatter(JsonFormatter())
        root_logger.addHandler(file_handler)
    except OSError as e:
        # Em caso de erro de disco ou permissão, log no console e segue em frente
        root_logger.warning("Falha ao configurar file logger (disco readonly?): %s", e)


def get_logger(name: str) -> logging.Logger:
    """Logger nomeado por componente (ex: `get_logger("domain.events")`)."""
    return logging.getLogger(name)
