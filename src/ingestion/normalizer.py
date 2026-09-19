"""Normalização dos eventos reais do TikTokLive para o domínio interno.

Este módulo é a única parte que conhece os nomes dos atributos da biblioteca
TikTokLive 7.x. Os objetos externos não atravessam esta fronteira.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.domain.events import Event, EventStatus, EventType, EventUser
from src.errors import AppError

MAX_COMMENT_LENGTH = 500


class EventNormalizationError(AppError):
    """Payload externo ausente, inválido ou fora dos limites aceitos."""


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _user(raw_event: Any) -> EventUser:
    user = getattr(raw_event, "user", None)
    if user is None:
        raise EventNormalizationError("evento TikTok sem usuário")

    nickname = _text(getattr(user, "nickname", ""))
    unique_id = _text(getattr(user, "unique_id", ""))
    display_name = nickname or unique_id
    if not display_name:
        raise EventNormalizationError("evento TikTok sem nome de usuário")

    source_id = getattr(user, "id", None)
    external_id = str(source_id) if source_id not in (None, "", 0) else None
    return EventUser(display_name=display_name, external_id=external_id)


def _source_timestamp(raw_event: Any, received_at: datetime) -> datetime:
    common = getattr(raw_event, "common", None)
    create_time = getattr(common, "create_time", None)
    if isinstance(create_time, int) and create_time > 0:
        # CommonMessageData.create_time é epoch em milissegundos na versão
        # verificada do protocolo. Valores menores são tratados como segundos
        # para manter o adapter defensivo contra fixtures simplificadas.
        seconds = create_time / 1000 if create_time >= 10**11 else create_time
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    return received_at


def _event_base(raw_event: Any, event_type: EventType, received_at: datetime) -> dict[str, Any]:
    common = getattr(raw_event, "common", None)
    source_event_id = getattr(common, "msg_id", None)
    if source_event_id in (None, "", 0):
        source_event_id = None
    return {
        "event_type": event_type,
        "source": "tiktok",
        "user": _user(raw_event),
        "event_id": None,
        "source_event_id": str(source_event_id) if source_event_id is not None else None,
        "timestamp": _source_timestamp(raw_event, received_at),
        "received_at": received_at,
        "status": EventStatus.NORMALIZED,
    }


def normalize_comment(raw_event: Any, *, received_at: datetime | None = None) -> Event:
    received_at = received_at or datetime.now(timezone.utc)
    content = getattr(raw_event, "content", None)
    if not isinstance(content, str):
        raise EventNormalizationError("comentário sem conteúdo textual")
    content = content.strip()
    if not content:
        raise EventNormalizationError("comentário vazio")
    if len(content) > MAX_COMMENT_LENGTH:
        raise EventNormalizationError("comentário excede o limite permitido")

    fields = _event_base(raw_event, EventType.COMMENT, received_at)
    fields["payload"] = {"text": content}
    fields.pop("event_id")
    return Event(**fields)


def normalize_gift(raw_event: Any, *, received_at: datetime | None = None) -> Event:
    received_at = received_at or datetime.now(timezone.utc)
    gift_id = getattr(raw_event, "gift_id", None)
    if not isinstance(gift_id, int) or gift_id <= 0:
        raise EventNormalizationError("gift sem gift_id válido")

    gift = getattr(raw_event, "gift", None)
    gift_name = _text(getattr(gift, "name", "")) or None
    fields = _event_base(raw_event, EventType.GIFT, received_at)
    fields["payload"] = {
        "gift_id": gift_id,
        "gift_name": gift_name,
        "repeat_count": getattr(raw_event, "repeat_count", 0),
        "combo_count": getattr(raw_event, "combo_count", 0),
        "repeat_end": getattr(raw_event, "repeat_end", 0),
    }
    fields.pop("event_id")
    return Event(**fields)


def normalize_follow(raw_event: Any, *, received_at: datetime | None = None) -> Event:
    received_at = received_at or datetime.now(timezone.utc)
    fields = _event_base(raw_event, EventType.FOLLOW, received_at)
    fields["payload"] = {"follow_count": getattr(raw_event, "follow_count", 0)}
    fields.pop("event_id")
    return Event(**fields)
