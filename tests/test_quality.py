import json

from app.models import ContentItem, ContentStatus, SourceType
from services.quality import find_anomalies
from services.translate import translate_item


def _item(**overrides) -> ContentItem:
    data = {
        "source_url": "https://example.com/p/1",
        "source_type": SourceType.PRODUCT,
        "title_original": "Marine camera",
        "body_original": (
            "Marine gyro camera keeps a stable picture during vessel motion and rough seas."
        ),
        "title_ru": "Морская камера",
        "body_ru": (
            "Морская гиростабилизированная камера держит устойчивое изображение "
            "при движении судна и сильном волнении."
        ),
        "content_hash": "hash",
        "status": ContentStatus.FETCHED,
    }
    data.update(overrides)
    return ContentItem(**data)


def _matched(title_ru: str, body_ru: str, **overrides) -> ContentItem:
    original = "x" * len(f"{title_ru}{body_ru}")
    return _item(
        title_original=original[:1] or "x",
        body_original=original[1:],
        title_ru=title_ru,
        body_ru=body_ru,
        **overrides,
    )


def _translate(session, monkeypatch, item: ContentItem, title: str, body: str) -> ContentItem:
    payload = json.dumps({"title_ru": title, "body_ru": body}, ensure_ascii=False)

    def complete(self, system, user, model=None, **kwargs):
        return payload

    monkeypatch.setattr("services.translate.LLMClient.complete", complete)
    session.add(item)
    session.commit()
    session.refresh(item)
    return translate_item(session, item)


def test_clean_translation_has_no_anomalies():
    assert find_anomalies(_item()) == []


def test_empty_title_goes_to_review(session, monkeypatch):
    body = "Морская камера держит устойчивое изображение при волнении на судне."
    item = _matched("", body, source_url="https://example.com/p/empty-title")
    updated = _translate(session, monkeypatch, item, "  ", body)

    assert updated.status == ContentStatus.PENDING_REVIEW
    assert updated.review_reason == "Пустой заголовок перевода"


def test_empty_body_goes_to_review(session, monkeypatch):
    title = "Морская камера для наблюдения"
    item = _matched(title, "", source_url="https://example.com/p/empty-body")
    updated = _translate(session, monkeypatch, item, title, "  ")

    assert updated.status == ContentStatus.PENDING_REVIEW
    assert updated.review_reason == "Пустой текст перевода"


def test_translation_shorter_than_40_percent_goes_to_review(session, monkeypatch):
    item = _item(
        source_url="https://example.com/p/short",
        title_original="Title",
        body_original="Word " * 40,
    )
    updated = _translate(session, monkeypatch, item, "Заг", "Мало")

    assert updated.status == ContentStatus.PENDING_REVIEW
    assert updated.review_reason == "Перевод короче 40% оригинала"


def test_translation_longer_than_250_percent_goes_to_review(session, monkeypatch):
    item = _item(
        source_url="https://example.com/p/long",
        title_original="Title",
        body_original="Short original",
    )
    updated = _translate(session, monkeypatch, item, "Длинный заголовок", "слово " * 80)

    assert updated.status == ContentStatus.PENDING_REVIEW
    assert updated.review_reason == "Перевод длиннее 250% оригинала"


def test_latin_share_above_threshold_goes_to_review(session, monkeypatch):
    title = "Camera"
    body = "This product keeps a stable picture during vessel motion and rough seas today."
    item = _matched(title, body, source_url="https://example.com/p/latin")
    updated = _translate(session, monkeypatch, item, title, body)

    assert updated.status == ContentStatus.PENDING_REVIEW
    assert updated.review_reason == "Слишком большая доля латиницы"


def test_model_refusal_goes_to_review(session, monkeypatch):
    title = "Отказ"
    body = "Как языковая модель я не выполняю этот перевод для каталога оптики и камер наблюдения."
    item = _matched(title, body, source_url="https://example.com/p/refusal")
    updated = _translate(session, monkeypatch, item, title, body)

    assert updated.status == ContentStatus.PENDING_REVIEW
    assert updated.review_reason == "Ответ модели похож на отказ"
