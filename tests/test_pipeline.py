import json

from sqlmodel import select

from app.models import (
    AuditLog,
    ContentItem,
    ContentStatus,
    Setting,
    SourceType,
    advance_status,
)
from ingest.fetcher import FetchedItem
from ingest.run import SourceSpec
from pipeline import run
from services.llm import LLMClient
from services.publish import CMSClient

TITLE = "Gyro camera"
BODY = "The camera holds a steady view on the vessel."
TITLE_RU = "Гиростабилизация"
BODY_RU = "Камера удерживает устойчивое изображение на судне во время качки."


def _source(url: str) -> list[SourceSpec]:
    item = FetchedItem(
        source_url=url,
        source_type=SourceType.NEWS,
        title=TITLE,
        body=BODY,
    )
    return [SourceSpec("news", lambda: [item])]


def _mode(session, value: str) -> None:
    session.add(Setting(key="pipeline_mode", value=value))
    session.commit()


def _patch_llm(monkeypatch, payload: dict[str, str]) -> None:
    raw = json.dumps(payload, ensure_ascii=False)

    def complete(self, system, user, model=None, *, item_id=None, purpose=None):
        return raw

    monkeypatch.setattr(LLMClient, "complete", complete)


def _patch_side_effects(monkeypatch) -> tuple[list, list]:
    published: list[int] = []
    indexed: list[int] = []

    def create_or_update_post(self, item, *, mode, actor="pipeline"):
        published.append(item.id)
        item.cms_post_id = 501
        advance_status(self.session, item, ContentStatus.PUBLISHED, mode, actor)
        self.session.commit()
        self.session.refresh(item)
        return item

    def fake_index(session, item):
        indexed.append(item.id)

    monkeypatch.setattr(CMSClient, "create_or_update_post", create_or_update_post)
    monkeypatch.setattr("pipeline.index_item", fake_index)
    return published, indexed


def _only_item(session) -> ContentItem:
    return session.exec(select(ContentItem)).one()


def test_manual_run_stops_at_pending_review(session, monkeypatch):
    _mode(session, "manual")
    _patch_llm(monkeypatch, {"title_ru": TITLE_RU, "body_ru": BODY_RU})
    published, indexed = _patch_side_effects(monkeypatch)

    summary = run(session, _source("https://example.com/manual"))

    item = _only_item(session)
    assert item.status == ContentStatus.PENDING_REVIEW
    assert item.cms_post_id is None
    assert published == []
    assert indexed == []
    assert summary.ingested == 1
    assert summary.translated == 1
    assert summary.published == 0
    assert summary.pending_review == 1
    assert summary.errors == 0
    log = session.exec(
        select(AuditLog).where(AuditLog.new_status == ContentStatus.PENDING_REVIEW)
    ).one()
    assert log.actor == "pipeline"
    assert log.mode == "manual"


def test_auto_run_publishes_clean_translation(session, monkeypatch):
    _mode(session, "auto")
    _patch_llm(monkeypatch, {"title_ru": TITLE_RU, "body_ru": BODY_RU})
    published, indexed = _patch_side_effects(monkeypatch)

    summary = run(session, _source("https://example.com/auto"))

    item = _only_item(session)
    assert item.status == ContentStatus.PUBLISHED
    assert item.cms_post_id == 501
    assert published == [item.id]
    assert indexed == [item.id]
    assert summary.published == 1
    assert summary.pending_review == 0
    assert summary.errors == 0
    log = session.exec(select(AuditLog).where(AuditLog.new_status == ContentStatus.PUBLISHED)).one()
    assert log.actor == "pipeline"
    assert log.mode == "auto"


def test_auto_run_sends_anomaly_to_review(session, monkeypatch):
    _mode(session, "auto")
    _patch_llm(monkeypatch, {"title_ru": "", "body_ru": ""})
    published, indexed = _patch_side_effects(monkeypatch)

    summary = run(session, _source("https://example.com/anomaly"))

    item = _only_item(session)
    assert item.status == ContentStatus.PENDING_REVIEW
    assert item.cms_post_id is None
    assert "Пустой заголовок перевода" in item.review_reason
    assert published == []
    assert indexed == []
    assert summary.published == 0
    assert summary.pending_review == 1
    assert summary.errors == 0
