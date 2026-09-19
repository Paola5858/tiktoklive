"""Testes unitários do DeduplicationCache.

Cobertos:
- Duplicata conhecida é bloqueada
- Eventos legítimos parecidos passam (usuário diferente = não duplicata)
- source_event_id tem precedência sobre deduplication_key()
- TTL funciona — evento expirado é tratado como novo
- Cache não cresce além de maxsize (LRU eviction)
- evict_expired() remove apenas expirados
- clear() esvazia o cache
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from src.domain.events import Event, EventType, EventUser
from src.engine.dedup import DeduplicationCache


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _user(name: str = "tester", uid: str = "123") -> EventUser:
    return EventUser(display_name=name, external_id=uid)


def _event(
    event_type: EventType = EventType.COMMENT,
    name: str = "tester",
    uid: str = "123",
    text: str = "ola",
    source_event_id: str | None = None,
) -> Event:
    return Event(
        event_type=event_type,
        source="tiktok",
        user=_user(name, uid),
        payload={"text": text} if event_type == EventType.COMMENT else {},
        source_event_id=source_event_id,
        timestamp=datetime.now(timezone.utc),
        received_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

def test_same_source_event_id_is_duplicate():
    """Dois eventos com mesmo source_event_id são duplicatas."""
    cache = DeduplicationCache(maxsize=100, ttl_seconds=60.0)
    e1 = _event(source_event_id="ext-123")
    e2 = _event(source_event_id="ext-123")

    assert cache.is_duplicate(e1) is False  # Primeiro: registra
    assert cache.is_duplicate(e2) is True   # Segundo: duplicata


def test_different_source_event_ids_are_not_duplicates():
    """Eventos com source_event_id diferentes não são duplicatas."""
    cache = DeduplicationCache(maxsize=100, ttl_seconds=60.0)
    e1 = _event(source_event_id="ext-001")
    e2 = _event(source_event_id="ext-002")

    assert cache.is_duplicate(e1) is False
    assert cache.is_duplicate(e2) is False


def test_same_content_same_user_without_source_id_is_duplicate():
    """Mesma fonte + tipo + usuário + conteúdo → duplicata (sem source_event_id)."""
    cache = DeduplicationCache(maxsize=100, ttl_seconds=60.0)
    e1 = _event(text="ola mundo")
    e2 = _event(text="ola mundo")  # mesmo conteúdo, mesmo usuário

    assert cache.is_duplicate(e1) is False
    assert cache.is_duplicate(e2) is True


def test_same_text_different_users_is_not_duplicate():
    """Mesmo comentário de usuários diferentes NÃO é duplicata."""
    cache = DeduplicationCache(maxsize=100, ttl_seconds=60.0)
    e1 = _event(name="joao", uid="001", text="ola")
    e2 = _event(name="maria", uid="002", text="ola")

    assert cache.is_duplicate(e1) is False
    assert cache.is_duplicate(e2) is False


def test_source_event_id_takes_precedence_over_content_key():
    """source_event_id tem precedência — mesmo payload mas IDs diferentes = eventos distintos."""
    cache = DeduplicationCache(maxsize=100, ttl_seconds=60.0)
    e1 = _event(text="ola", source_event_id="id-001")
    e2 = _event(text="ola", source_event_id="id-002")

    # Mesmo payload mas source_event_id diferente → eventos distintos
    assert cache.is_duplicate(e1) is False
    assert cache.is_duplicate(e2) is False


# ---------------------------------------------------------------------------
# TTL
# ---------------------------------------------------------------------------

def test_event_after_ttl_is_not_duplicate(monkeypatch):
    """Evento com TTL expirado é tratado como novo."""
    cache = DeduplicationCache(maxsize=100, ttl_seconds=1.0)

    # Simular inserção no passado (2 segundos atrás)
    original_time = time.monotonic
    call_count = 0

    def fake_time():
        nonlocal call_count
        call_count += 1
        # Primeira chamada (no is_duplicate): tempo no passado
        if call_count == 1:
            return original_time() - 2.0
        return original_time()

    monkeypatch.setattr("src.engine.dedup.time.monotonic", fake_time)

    e = _event(source_event_id="ext-ttl")
    cache.is_duplicate(e)  # Registra com timestamp "antigo"

    # Agora o tempo é atual — TTL deve ter expirado
    assert cache.is_duplicate(e) is False  # Tratado como novo


def test_event_within_ttl_is_duplicate():
    """Evento dentro do TTL ainda é duplicata."""
    cache = DeduplicationCache(maxsize=100, ttl_seconds=60.0)
    e = _event(source_event_id="ext-within-ttl")

    cache.is_duplicate(e)  # Registra
    assert cache.is_duplicate(e) is True  # Ainda dentro do TTL


# ---------------------------------------------------------------------------
# Cache size limit (LRU eviction)
# ---------------------------------------------------------------------------

def test_cache_does_not_exceed_maxsize():
    """Cache LRU não excede o maxsize configurado."""
    maxsize = 10
    cache = DeduplicationCache(maxsize=maxsize, ttl_seconds=60.0)

    # Inserir mais eventos do que o maxsize
    for i in range(maxsize + 5):
        e = _event(source_event_id=f"ext-{i}")
        cache.is_duplicate(e)

    assert cache.size <= maxsize


def test_lru_eviction_removes_oldest():
    """LRU eviction remove o mais antigo quando o cache está cheio."""
    cache = DeduplicationCache(maxsize=3, ttl_seconds=60.0)

    e0 = _event(source_event_id="ext-0")
    e1 = _event(source_event_id="ext-1")
    e2 = _event(source_event_id="ext-2")
    e3 = _event(source_event_id="ext-3")

    # Inserir 3 eventos (cache cheio)
    cache.is_duplicate(e0)
    cache.is_duplicate(e1)
    cache.is_duplicate(e2)

    # Inserir o 4º: e0 deve ser evicted (mais antigo)
    cache.is_duplicate(e3)

    # e1, e2, e3 ainda estão → são duplicatas
    assert cache.is_duplicate(e1) is True
    assert cache.is_duplicate(e2) is True

    # e0 não está mais no cache → não é duplicata
    assert cache.is_duplicate(e0) is False


# ---------------------------------------------------------------------------
# evict_expired
# ---------------------------------------------------------------------------

def test_evict_expired_removes_stale_entries(monkeypatch):
    """evict_expired() remove entradas com TTL expirado."""
    cache = DeduplicationCache(maxsize=100, ttl_seconds=1.0)

    # Inserir 3 eventos "antigos" e 2 "recentes"
    original_time = time.monotonic
    stale_time = original_time() - 2.0  # 2 segundos atrás

    inserted = []
    for i in range(3):
        e = _event(source_event_id=f"stale-{i}")
        # Inserção manual com timestamp antigo
        key = f"tiktok:sid:stale-{i}"
        from src.engine.dedup import _CacheEntry
        cache._cache[key] = _CacheEntry(inserted_at=stale_time)
        inserted.append(key)

    # 2 eventos recentes
    for i in range(2):
        e = _event(source_event_id=f"fresh-{i}")
        cache.is_duplicate(e)

    initial_size = cache.size
    removed = cache.evict_expired()

    assert removed == 3
    assert cache.size == initial_size - 3


def test_evict_expired_does_not_remove_valid_entries():
    """evict_expired() não remove entradas ainda dentro do TTL."""
    cache = DeduplicationCache(maxsize=100, ttl_seconds=60.0)
    for i in range(5):
        e = _event(source_event_id=f"valid-{i}")
        cache.is_duplicate(e)

    removed = cache.evict_expired()
    assert removed == 0
    assert cache.size == 5


# ---------------------------------------------------------------------------
# clear()
# ---------------------------------------------------------------------------

def test_clear_empties_cache():
    """clear() remove todas as entradas."""
    cache = DeduplicationCache(maxsize=100, ttl_seconds=60.0)
    for i in range(10):
        e = _event(source_event_id=f"ext-{i}")
        cache.is_duplicate(e)

    cache.clear()
    assert cache.size == 0


def test_after_clear_same_event_is_not_duplicate():
    """Após clear(), mesmo evento não é mais duplicata."""
    cache = DeduplicationCache(maxsize=100, ttl_seconds=60.0)
    e = _event(source_event_id="ext-clear-test")
    cache.is_duplicate(e)  # Registra
    cache.clear()
    assert cache.is_duplicate(e) is False  # Não é mais duplicata
