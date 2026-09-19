"""Configuração de logging estruturado.

Objetivo: observabilidade desde o início, sem virar `print` espalhado
pelo código nem vazar segredos nos logs.
"""

from __future__ import annotations

import logging

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def setup_logging(level: str) -> None:
    """Configura o logging raiz da aplicação. Chamar uma única vez, na inicialização."""
    logging.basicConfig(level=level, format=_FORMAT)


def get_logger(name: str) -> logging.Logger:
    """Logger nomeado por componente (ex: `get_logger("domain.events")`)."""
    return logging.getLogger(name)
