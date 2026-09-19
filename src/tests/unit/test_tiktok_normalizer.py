from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.domain.events import EventStatus, EventType
from src.ingestion.normalizer import (
    EventNormalizationError,
    normalize_comment,
    normalize_follow,
    normalize_gift,
)


RECEIVED_AT = datetime(2026, 9, 19, 18, 0, tzinfo=timezone.utc)


def _user(**overrides):
    values = {"id": 123, "nickname": "Paola", "unique_id": "paola"}
    values.update(overrides)
    return SimpleNamespace(**values)


def _common(**overrides):
    values = {"msg_id": 456, "create_time": 1_758_298_800_000}
    values.update(overrides)
    return SimpleNamespace(**values)


def test_normalize_comment_preserves_identity_and_timestamps():
    raw = SimpleNamespace(user=_user(), common=_common(), content=" oi ")

    event = normalize_comment(raw, received_at=RECEIVED_AT)

    assert event.event_type is EventType.COMMENT
    assert event.status is EventStatus.NORMALIZED
    assert event.user.external_id == "123"
    assert event.user.display_name == "Paola"
    assert event.source_event_id == "456"
    assert event.timestamp.tzinfo is not None
    assert event.received_at == RECEIVED_AT
    assert event.payload == {"text": "oi"}


def test_normalize_gift_uses_verified_gift_fields():
    raw = SimpleNamespace(
        user=_user(),
        common=_common(),
        gift_id=321,
        repeat_count=2,
        combo_count=3,
        repeat_end=1,
        gift=SimpleNamespace(name="Rose"),
    )

    event = normalize_gift(raw, received_at=RECEIVED_AT)

    assert event.event_type is EventType.GIFT
    assert event.payload == {
        "gift_id": 321,
        "gift_name": "Rose",
        "repeat_count": 2,
        "combo_count": 3,
        "repeat_end": 1,
    }


def test_normalize_follow_does_not_invent_missing_user_id():
    raw = SimpleNamespace(
        user=_user(id=0, nickname="Paola", unique_id="paola"),
        common=_common(),
        follow_count=4,
    )

    event = normalize_follow(raw, received_at=RECEIVED_AT)

    assert event.user.external_id is None
    assert event.payload == {"follow_count": 4}


@pytest.mark.parametrize(
    "raw, message",
    [
        (SimpleNamespace(user=None, common=_common(), content="oi"), "sem usuário"),
        (SimpleNamespace(user=_user(), common=_common(), content=""), "comentário vazio"),
        (SimpleNamespace(user=_user(), common=_common(), content="x" * 501), "excede"),
    ],
)
def test_normalize_comment_rejects_invalid_payloads(raw, message):
    with pytest.raises(EventNormalizationError, match=message):
        normalize_comment(raw, received_at=RECEIVED_AT)


def test_normalize_gift_rejects_invalid_gift_id():
    raw = SimpleNamespace(user=_user(), common=_common(), gift_id=0, gift=None)

    with pytest.raises(EventNormalizationError, match="gift_id"):
        normalize_gift(raw, received_at=RECEIVED_AT)
