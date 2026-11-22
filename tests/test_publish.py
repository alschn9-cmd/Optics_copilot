import json

import httpx
import pytest
from sqlmodel import select

from app.models import AuditLog, ContentItem, ContentStatus, SourceType, advance_status
from app.settings import Settings
from services.publish import CMSClient


def _settings() -> Settings:
    return Settings(
        wp_url="https://cms.example/",
        wp_user="editor",
        wp_app_password="app pass",
        _env_file=None,
    )


def _translated(session, url: str = "https://example.com/item") -> ContentItem:
    item = ContentItem(
        source_url=url,
        source_type=SourceType.NEWS,
        title_original="Gyro camera",
        body_original="Steady view.",
        title_ru="Гирокамера",
        body_ru="Устойчивый вид.",
        status=ContentStatus.FETCHED,
        content_hash=url,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    advance_status(session, item, ContentStatus.TRANSLATED, "auto", "test")
    session.commit()
    return item


def test_create_then_update_reuses_cms_post_id(session):
    item = _translated(session)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        payload = json.loads(request.content)
        assert request.headers["authorization"].startswith("Basic ")
        assert payload["title"] == "Гирокамера"
        assert payload["content"] == "Устойчивый вид."
        assert payload["status"] == "publish"
        return httpx.Response(200, json={"id": 7})

    client = CMSClient(
        session,
        settings=_settings(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.create_or_update_post(item, mode="auto", actor="pipeline")
    assert item.cms_post_id == 7
    assert item.status == ContentStatus.PUBLISHED

    client.create_or_update_post(item, mode="auto", actor="pipeline")
    assert calls == ["/wp-json/wp/v2/posts", "/wp-json/wp/v2/posts/7"]
    assert item.cms_post_id == 7
    assert item.status == ContentStatus.PUBLISHED
    published = AuditLog.new_status == ContentStatus.PUBLISHED
    logs = session.exec(select(AuditLog).where(published)).all()
    assert len(logs) == 1
    assert logs[0].actor == "pipeline"


def test_unpublish_sets_wordpress_draft(session):
    item = _translated(session)
    item.cms_post_id = 9
    session.add(item)
    session.commit()
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 9, "status": "draft"})

    client = CMSClient(
        session,
        settings=_settings(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.unpublish(item)
    assert seen == {"path": "/wp-json/wp/v2/posts/9", "payload": {"status": "draft"}}
    assert item.status == ContentStatus.TRANSLATED


def test_unpublish_requires_cms_post_id(session):
    item = _translated(session)
    client = CMSClient(session, settings=_settings(), http_client=httpx.Client())
    with pytest.raises(ValueError, match="cms_post_id"):
        client.unpublish(item)
