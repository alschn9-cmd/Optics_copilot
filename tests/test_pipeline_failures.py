import json
import logging
from pathlib import Path

import httpx
from pypdf import PdfWriter
from sqlmodel import select

from app.models import ContentItem, ContentStatus, Setting, SourceType, advance_status
from ingest.fetcher import FetchedItem, fetch_news, fetch_pdf, parse_news_html
from ingest.run import SourceSpec
from pipeline import run
from services.llm import LLMClient
from services.publish import CMSClient

PAGE = "https://supplier.example/news/"
TITLE = "Gyro camera"
BODY = "The camera holds a steady view on the vessel."
TITLE_RU = "Гиростабилизация"
BODY_RU = "Камера удерживает устойчивое изображение на судне во время качки."
FIXTURES = Path(__file__).parent / "fixtures"
RSS = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item>
    <title>{TITLE}</title>
    <link>https://supplier.example/news/gyro/</link>
    <description>{BODY}</description>
  </item>
</channel></rss>
"""


def _mode(session, value: str) -> None:
    session.add(Setting(key="pipeline_mode", value=value))
    session.commit()


def _rows(session) -> list[ContentItem]:
    return list(session.exec(select(ContentItem).order_by(ContentItem.id)).all())


def _patch_llm(monkeypatch, answers: list[str]) -> None:
    pending = list(answers)

    def complete(self, system, user, model=None, *, item_id=None, purpose=None):
        return pending.pop(0)

    monkeypatch.setattr(LLMClient, "complete", complete)


def _news_source(phases: list[str | int]) -> list[SourceSpec]:
    state = {"index": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        phase = phases[min(state["index"], len(phases) - 1)]
        if isinstance(phase, int):
            return httpx.Response(phase, text="unavailable")
        return httpx.Response(200, text=phase)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    def fetch() -> list[FetchedItem]:
        try:
            return fetch_news(PAGE, client=client)
        finally:
            state["index"] += 1

    return [SourceSpec("news", fetch)]


def _stored_news() -> list[SourceSpec]:
    item = FetchedItem(
        source_url="https://supplier.example/news/gyro/",
        source_type=SourceType.NEWS,
        title=TITLE,
        body=BODY,
    )
    return [SourceSpec("news", lambda: [item])]


def test_supplier_http_500_is_retried_on_the_next_run(session, monkeypatch, caplog):
    _mode(session, "manual")
    _patch_llm(monkeypatch, [json.dumps({"title_ru": TITLE_RU, "body_ru": BODY_RU})])
    sources = _news_source([500, RSS])

    with caplog.at_level(logging.ERROR):
        failed = run(session, sources)
    assert failed.errors >= 1
    assert _rows(session) == []
    assert "source failed" in caplog.text

    recovered = run(session, sources)
    stored = _rows(session)
    assert len(stored) == 1
    assert stored[0].status == ContentStatus.PENDING_REVIEW
    assert recovered.errors == 0


def test_empty_supplier_page_is_retried_on_the_next_run(session, monkeypatch, caplog):
    _mode(session, "manual")
    _patch_llm(monkeypatch, [json.dumps({"title_ru": TITLE_RU, "body_ru": BODY_RU})])
    sources = _news_source(["<html><body></body></html>", RSS])

    with caplog.at_level(logging.WARNING):
        failed = run(session, sources)
    assert _rows(session) == []
    assert "no news items" in caplog.text
    assert failed.ingested == 0

    run(session, sources)
    stored = _rows(session)
    assert len(stored) == 1
    assert stored[0].status == ContentStatus.PENDING_REVIEW


def test_redesigned_supplier_html_is_retried_on_the_next_run(session, monkeypatch, caplog):
    broken = (FIXTURES / "broken_layout.html").read_text(encoding="utf-8")
    assert parse_news_html(broken, PAGE) == []
    _mode(session, "manual")
    _patch_llm(monkeypatch, [json.dumps({"title_ru": TITLE_RU, "body_ru": BODY_RU})])
    sources = _news_source([broken, RSS])

    with caplog.at_level(logging.WARNING):
        run(session, sources)
    assert _rows(session) == []
    assert "no news items" in caplog.text

    run(session, sources)
    stored = _rows(session)
    assert len(stored) == 1
    assert stored[0].title_original == TITLE
    assert stored[0].status == ContentStatus.PENDING_REVIEW


def test_garbage_json_three_times_stays_fetched_until_a_later_run(session, monkeypatch, caplog):
    _mode(session, "manual")
    good = json.dumps({"title_ru": TITLE_RU, "body_ru": BODY_RU})
    _patch_llm(monkeypatch, ["мусор", "всё ещё мусор", "третий мусор", good])

    with caplog.at_level(logging.ERROR):
        failed = run(session, _stored_news())
    stored = _rows(session)
    assert len(stored) == 1
    assert stored[0].status == ContentStatus.FETCHED
    assert stored[0].title_ru == ""
    assert failed.errors >= 1
    assert "failed to translate" in caplog.text

    recovered = run(session, _stored_news())
    session.refresh(stored[0])
    assert stored[0].status == ContentStatus.PENDING_REVIEW
    assert stored[0].title_ru == TITLE_RU
    assert recovered.translated == 1


def test_cms_outage_keeps_the_item_for_the_next_publish(session, monkeypatch, caplog):
    _mode(session, "auto")
    _patch_llm(monkeypatch, [json.dumps({"title_ru": TITLE_RU, "body_ru": BODY_RU})])
    calls = {"publish": 0}

    def create_or_update_post(self, item, *, mode, actor="pipeline"):
        calls["publish"] += 1
        if calls["publish"] == 1:
            raise httpx.ConnectError("cms down")
        item.cms_post_id = 15
        advance_status(self.session, item, ContentStatus.PUBLISHED, mode, actor)
        self.session.commit()
        return item

    monkeypatch.setattr(CMSClient, "create_or_update_post", create_or_update_post)
    monkeypatch.setattr("pipeline.index_item", lambda db, item: None)

    with caplog.at_level(logging.ERROR):
        failed = run(session, _stored_news())
    stored = _rows(session)
    assert len(stored) == 1
    item_id = stored[0].id
    assert stored[0].status == ContentStatus.TRANSLATED
    assert stored[0].cms_post_id is None
    assert failed.published == 0
    assert "failed to publish" in caplog.text

    recovered = run(session, _stored_news())
    item = session.get(ContentItem, item_id)
    assert item is not None
    assert item.status == ContentStatus.PUBLISHED
    assert item.cms_post_id == 15
    assert recovered.published == 1
    assert len(_rows(session)) == 1


def test_pdf_without_text_layer_stays_fetched(session, monkeypatch, caplog, tmp_path):
    _mode(session, "manual")

    def complete(self, system, user, model=None, *, item_id=None, purpose=None):
        raise AssertionError("a scan must not be sent to the model")

    monkeypatch.setattr(LLMClient, "complete", complete)
    target = tmp_path / "scan.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with target.open("wb") as handle:
        writer.write(handle)
    pdf_bytes = target.read_bytes()
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=pdf_bytes))
    )
    sources = [
        SourceSpec("pdf", lambda: [fetch_pdf("https://supplier.example/scan.pdf", client=client)])
    ]

    with caplog.at_level(logging.ERROR):
        first = run(session, sources)
    stored = _rows(session)
    assert len(stored) == 1
    assert stored[0].needs_ocr is True
    assert stored[0].status == ContentStatus.FETCHED
    assert first.errors >= 1
    assert "no text layer" in caplog.text

    second = run(session, sources)
    session.refresh(stored[0])
    assert stored[0].status == ContentStatus.FETCHED
    assert len(_rows(session)) == 1
    assert second.published == 0
