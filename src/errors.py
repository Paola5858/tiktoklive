"""Erros de base da aplicação.

Erros específicos (domínio, configuração, infraestrutura) herdam daqui.
A ideia é nunca usar `Exception` genérica pra esconder problemas
diferentes — ver a seção `error_model` da spec da fase 1.
"""

from __future__ import annotations


class AppError(Exception):
    """Erro base de toda a aplicação."""


class ConfigurationError(AppError):
    """Configuração obrigatória ausente ou inválida."""
