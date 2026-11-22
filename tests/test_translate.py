import json

import pytest

from app.models import ContentItem, ContentStatus, SourceType, TokenPurpose
from services.translate import parse_translation, translate_item


def _item(**overrides) -> ContentItem:
    data = {
        "source_url": "https://example.com/news/1",
        "source_type": SourceType.NEWS,
        "title_original": "Marine camera announcement",
        "body_original": "The camera keeps a stable picture on a moving vessel in rough seas. " * 3,
        "content_hash": "abc",
        "status": ContentStatus.FETCHED,
    }
    data.update(overrides)
    return ContentItem(**data)


def _json(title: str, body: str) -> str:
    return json.dumps({"title_ru": title, "body_ru": body}, ensure_ascii=False)


def _patch_llm(monkeypatch, replies: list[str]) -> list[dict]:
    calls: list[dict] = []

    def complete(self, system, user, model=None, **kwargs):
        calls.append({"system": system, "user": user, "model": model, "kwargs": kwargs})
        if not replies:
            raise AssertionError("unexpected LLM call")
        return replies.pop(0)

    monkeypatch.setattr("services.translate.LLMClient.complete", complete)
    return calls


def test_successful_translation(session, monkeypatch):
    item = _item()
    session.add(item)
    session.commit()
    session.refresh(item)
    body = "Камера удерживает устойчивое изображение на движущемся судне при сильном волнении. " * 3
    calls = _patch_llm(monkeypatch, [_json("Морская камера", body)])

    updated = translate_item(session, item)

    assert updated.status == ContentStatus.TRANSLATED
    assert updated.title_ru == "Морская камера"
    assert updated.body_ru == body.strip()
    assert updated.review_reason == ""
    assert len(calls) == 1
    assert calls[0]["kwargs"]["purpose"] == TokenPurpose.TRANSLATE
    assert calls[0]["kwargs"]["item_id"] == item.id
    assert "JSON" in calls[0]["system"]


def test_invalid_json_is_retried_once(session, monkeypatch):
    item = _item(source_type=SourceType.PRODUCT, source_url="https://example.com/p/1")
    session.add(item)
    session.commit()
    session.refresh(item)
    body = "Стабильная морская камера с тепловизором для наблюдения за горизонтом и берегом. " * 2
    calls = _patch_llm(monkeypatch, ["не json", _json("Камера", body)])

    updated = translate_item(session, item)

    assert updated.status == ContentStatus.TRANSLATED
    assert updated.title_ru == "Камера"
    assert len(calls) == 2
    assert "валидный JSON" in calls[1]["user"]
    assert calls[1]["kwargs"]["purpose"] == TokenPurpose.TRANSLATE


def test_translate_item_rejects_other_statuses(session, monkeypatch):
    item = _item(status=ContentStatus.PUBLISHED)
    session.add(item)
    session.commit()
    calls = _patch_llm(monkeypatch, [])

    with pytest.raises(ValueError, match="Cannot translate"):
        translate_item(session, item)

    assert calls == []


def test_parse_translation_accepts_fenced_json():
    parsed = parse_translation('```json\n{"title_ru": "А", "body_ru": "Б"}\n```')
    assert parsed == {"title_ru": "А", "body_ru": "Б"}
