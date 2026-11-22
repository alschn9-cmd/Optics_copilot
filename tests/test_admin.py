from datetime import UTC, date, datetime, timedelta

import pytest
from sqlmodel import select

from admin.actions import (
    bot_metrics,
    funnel,
    last_run_errors,
    publish_from_review,
    reject_from_review,
    set_pipeline_mode,
    usage_report,
)
from app.models import (
    AuditLog,
    ChatMessage,
    ContentItem,
    ContentStatus,
    Setting,
    SourceType,
    TokenPurpose,
    TokenUsage,
    advance_status,
)
from pipeline import PIPELINE_MODE_KEY
from services.publish import CMSClient


def _pending(session, reason: str = "Слишком большая доля латиницы") -> ContentItem:
    item = ContentItem(
        source_url=f"https://example.com/{reason}",
        source_type=SourceType.NEWS,
        title_original="Gyro camera",
        body_original="Steady view.",
        title_ru="Черновик",
        body_ru="Текст",
        review_reason=reason,
        status=ContentStatus.FETCHED,
        content_hash=reason,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    advance_status(session, item, ContentStatus.PENDING_REVIEW, "manual", "translator")
    session.commit()
    session.refresh(item)
    return item


def test_publish_saves_edits_then_indexes(session, monkeypatch):
    item = _pending(session)
    calls: list[tuple] = []

    def create_or_update_post(self, target, *, mode, actor="pipeline"):
        calls.append(("cms", target.title_ru, target.body_ru, mode, actor))
        target.cms_post_id = 15
        advance_status(self.session, target, ContentStatus.PUBLISHED, mode, actor)
        self.session.commit()
        return target

    def upsert(db, target, client=None):
        calls.append(("index", target.id, target.status))

    monkeypatch.setattr(CMSClient, "create_or_update_post", create_or_update_post)
    monkeypatch.setattr("admin.actions.upsert_item", upsert)

    saved = publish_from_review(session, item.id, "  Заголовок  ", "  Готовый текст  ")

    assert saved.title_ru == "Заголовок"
    assert saved.body_ru == "Готовый текст"
    assert saved.status == ContentStatus.PUBLISHED
    assert saved.cms_post_id == 15
    assert calls == [
        ("cms", "Заголовок", "Готовый текст", "manual", "admin"),
        ("index", item.id, ContentStatus.PUBLISHED),
    ]
    log = session.exec(select(AuditLog).where(AuditLog.new_status == ContentStatus.PUBLISHED)).one()
    assert log.actor == "admin"
    assert log.mode == "manual"


def test_reject_sets_status_and_deletes_index(session, monkeypatch):
    item = _pending(session, reason="Пустой текст перевода")
    deleted: list[int] = []
    monkeypatch.setattr("admin.actions.delete_item", lambda item_id: deleted.append(item_id))

    saved = reject_from_review(session, item.id)

    assert saved.status == ContentStatus.REJECTED
    assert deleted == [item.id]
    log = session.exec(select(AuditLog).where(AuditLog.new_status == ContentStatus.REJECTED)).one()
    assert log.actor == "admin"
    assert log.mode == "manual"


def test_publish_and_reject_require_pending_review(session):
    item = ContentItem(
        source_url="https://example.com/published",
        source_type=SourceType.PRODUCT,
        title_original="Title",
        body_original="Body",
        status=ContentStatus.FETCHED,
        content_hash="published",
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    advance_status(session, item, ContentStatus.TRANSLATED, "manual", "test")
    session.commit()
    with pytest.raises(ValueError, match="pending review"):
        publish_from_review(session, item.id, "Заголовок", "Текст")
    with pytest.raises(ValueError, match="pending review"):
        reject_from_review(session, item.id)


def test_set_pipeline_mode_writes_setting(session):
    assert set_pipeline_mode(session, "auto") == "auto"
    assert session.get(Setting, PIPELINE_MODE_KEY).value == "auto"
    assert set_pipeline_mode(session, "manual") == "manual"
    assert session.get(Setting, PIPELINE_MODE_KEY).value == "manual"
    with pytest.raises(ValueError):
        set_pipeline_mode(session, "sometimes")


def test_dashboard_counts_for_the_period(session):
    today = datetime.now(UTC)
    yesterday = today - timedelta(days=1)
    item = _pending(session, reason="period")
    item.created_at = yesterday
    session.add(
        TokenUsage(
            item_id=item.id,
            model="test",
            input_tokens=10,
            output_tokens=0,
            cost_usd=0.25,
            purpose=TokenPurpose.TRANSLATE,
            created_at=yesterday,
        )
    )
    session.add(
        TokenUsage(
            item_id=item.id,
            model="test",
            input_tokens=4,
            output_tokens=2,
            cost_usd=0.75,
            purpose=TokenPurpose.CHAT,
            created_at=yesterday,
        )
    )
    session.add(ChatMessage(session_id="a", role="user", text="вопрос", created_at=yesterday))
    session.add(
        ChatMessage(
            session_id="a",
            role="assistant",
            text="Передаю ваш вопрос менеджеру.",
            escalated=True,
            created_at=yesterday,
        )
    )
    session.add(ChatMessage(session_id="b", role="user", text="ещё", created_at=yesterday))
    old = today - timedelta(days=10)
    session.add(ChatMessage(session_id="old", role="user", text="старое", created_at=old))
    session.commit()

    start = yesterday.date()
    end = today.date()
    stages = dict(funnel(session, start, end))
    assert stages["Забрано"] == 1
    assert stages["На ревью"] == 1
    usage = usage_report(session, start, end)
    assert usage["total"] == pytest.approx(1.0)
    assert usage["by_purpose"] == {"translate": pytest.approx(0.25), "chat": pytest.approx(0.75)}
    assert usage["by_day"] == [(yesterday.date().isoformat(), pytest.approx(1.0))]
    metrics = bot_metrics(session, start, end)
    assert metrics == {"dialogs": 2, "messages": 3, "escalated_share": pytest.approx(1 / 3)}
    assert bot_metrics(session, date(2000, 1, 1), date(2000, 1, 2))["messages"] == 0


def test_last_run_errors_keeps_only_the_latest_run():
    first = "ingested=1 translated=0 published=0 pending_review=0 errors=1"
    second = "ingested=0 translated=1 published=0 pending_review=0 errors=1"
    log = (
        "2026-01-01 00:00:00,000 ERROR pipeline: failed to translate item 1\n"
        "Traceback (most recent call last):\n"
        "ValueError: old\n"
        f"2026-01-01 00:00:01,000 INFO pipeline: pipeline run: {first}\n"
        "2026-01-02 00:00:02,000 ERROR pipeline: failed to publish item 2\n"
        "Traceback (most recent call last):\n"
        "CMSError: down\n"
        f"2026-01-02 00:00:03,000 INFO pipeline: pipeline run: {second}\n"
    )
    assert last_run_errors(log) == (
        "2026-01-02 00:00:02,000 ERROR pipeline: failed to publish item 2\n"
        "Traceback (most recent call last):\n"
        "CMSError: down"
    )
    assert last_run_errors("nothing happened") == ""
