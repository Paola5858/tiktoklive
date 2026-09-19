"""Cache de deduplicação com LRU + TTL.

Objetivo: evitar processamento duplicado sem apagar eventos legítimos
e sem crescimento infinito de memória.

Design:
- OrderedDict como LRU: move_to_end() para marcar acesso recente,
  popitem(last=False) para eviction do mais antigo quando cheio
- TTL: timestamp de inserção armazenado junto com a chave
  Expiração verificada na leitura (lazy) e na varredura periódica
- Chave de deduplicação:
  1. source_event_id quando disponível e confiável (string não-vazia)
  2. Event.deduplication_key() como fallback (hash do conteúdo relevante)
- Cache overflow: LRU eviction (mais antigo sai, independente de TTL)

Regras semânticas (ver spec):
- Eventos visualmente semelhantes podem ser eventos legítimos diferentes
- Comentários idênticos de usuários diferentes NÃO são duplicatas
- Comentários idênticos do MESMO usuário dentro da janela SÃO candidatos
  (mas a chave de dedupe já inclui o usuário, então isso é tratado)
- Gifts com mesmo source_event_id são duplicatas (callback repetido da lib)

Invariante de memória:
- _cache: OrderedDict com no máximo maxsize entradas — O(maxsize)
- Nenhuma coleção auxiliar sem limite
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.domain.events import Event


@dataclass(slots=True)
class _CacheEntry:
    """Entrada no cache de deduplicação."""
    inserted_at: float  # monotonic time


class DeduplicationCache:
    """Cache LRU com TTL para deduplicação de eventos.

    Thread-safety: não necessária — asyncio é single-threaded.
    """

    def __init__(self, maxsize: int = 10_000, ttl_seconds: float = 30.0) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize deve ser > 0")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds deve ser > 0")
        self._maxsize = maxsize
        self._ttl = ttl_seconds
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()

    @property
    def maxsize(self) -> int:
        return self._maxsize

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    @property
    def size(self) -> int:
        return len(self._cache)

    def is_duplicate(self, event: "Event") -> bool:
        """Verifica se o evento é duplicata e registra no cache se não for.

        Processo:
        1. Determina a chave de deduplicação
        2. Verifica se a chave está no cache e ainda dentro do TTL
        3. Se duplicata: retorna True (sem modificar o cache)
        4. Se novo: insere no cache (com possível eviction LRU) e retorna False

        Returns:
            True se o evento é duplicata e deve ser descartado
            False se o evento é novo e deve prosseguir
        """
        key = self._dedup_key(event)
        now = time.monotonic()

        if key in self._cache:
            entry = self._cache[key]
            if now - entry.inserted_at < self._ttl:
                # Duplicata dentro do TTL — move para o fim (LRU: recentemente acessado)
                self._cache.move_to_end(key)
                return True
            else:
                # Expirado — remover e tratar como evento novo
                del self._cache[key]

        # Evento novo: inserir no cache
        self._insert(key, now)
        return False

    def evict_expired(self) -> int:
        """Remove todas as entradas expiradas do cache.

        Deve ser chamado periodicamente pelo engine (não por evento individual
        para evitar overhead a cada evento). Retorna o número de entradas removidas.

        Percorre do mais antigo ao mais recente (OrderedDict) e para assim
        que encontrar uma entrada ainda válida — os mais antigos expiram primeiro.
        """
        now = time.monotonic()
        expired_keys: list[str] = []

        for key, entry in self._cache.items():
            if now - entry.inserted_at >= self._ttl:
                expired_keys.append(key)
            else:
                # Como o OrderedDict é ordenado por inserção (mais antigos primeiro),
                # podemos parar assim que encontramos uma entrada válida
                break

        for key in expired_keys:
            del self._cache[key]

        return len(expired_keys)

    def clear(self) -> None:
        """Remove todas as entradas do cache."""
        self._cache.clear()

    def snapshot(self) -> dict[str, object]:
        """Retorna estado atual do cache para observabilidade."""
        return {
            "size": len(self._cache),
            "maxsize": self._maxsize,
            "ttl_seconds": self._ttl,
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _dedup_key(self, event: "Event") -> str:
        """Determina a chave de deduplicação para um evento.

        source_event_id tem precedência quando disponível porque é
        a identidade fornecida pela origem (mais confiável que o hash do payload).
        Fallback para Event.deduplication_key() quando source_event_id é None.
        """
        if event.source_event_id:
            # Prefixar com source para evitar colisão entre origens distintas
            return f"{event.source}:sid:{event.source_event_id}"
        return event.deduplication_key()

    def _insert(self, key: str, now: float) -> None:
        """Insere uma chave no cache, aplicando LRU eviction se necessário."""
        if len(self._cache) >= self._maxsize:
            # LRU eviction: remove o mais antigo (primeiro do OrderedDict)
            self._cache.popitem(last=False)

        self._cache[key] = _CacheEntry(inserted_at=now)
