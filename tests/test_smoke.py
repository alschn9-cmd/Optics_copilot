import json
from contextlib import contextmanager
from pathlib import Path

import chromadb
import httpx
from fastapi.testclient import TestClient
from sqlmodel import select

import services.llm as llm_module
import services.publish as publish_module
from admin.actions import publish_from_review, set_pipeline_mode
from app.db import get_session
from app.models import ChatMessage, ContentItem, ContentStatus
from bot.api import app
from ingest.fetcher import fetch_products
from ingest.run import SourceSpec
from pipeline import run

FIXTURES = Path(__file__).parent / "fixtures"
LISTING = "https://www.ryanoptics.com/Marine-Gyro-Stabilized-Camera-pl44267786.html"
PRODUCT = "https://www.ryanoptics.com/Ultra-Precision-Globe-Shaped-Gyro-Stabilizer-pd535938348.html"
GLOBE_TITLE = "Сверхточный шаровой гиростабилизатор"
GLOBE_SENTENCE = "Шаровая система гиростабилизации удерживает изображение, фокус 6 мм. "
CARD_TITLE = "Карточка товара"
CARD_SENTENCE = "Описание гиростабилизированной камеры для морского наблюдения. "
QUESTION = "Какое фокусное расстояние у шаровой гиростабилизации?"
ANSWER = "Фокусное расстояние шаровой системы гиростабилизации — 6 мм."
CHAT = "smoke-deploy"


def test_smoke_from_fixture_to_bot_answer(session, monkeypatch):
    set_pipeline_mode(session, "manual")
    listing = (FIXTURES / "ryanoptics_products.html").read_text(encoding="utf-8")
    detail = (FIXTURES / "ryanoptics_product.html").read_text(encoding="utf-8")
    collection = _collection(monkeypatch)
    seen: dict[str, str] = {}
    cms_titles: list[str] = []
    _mock_http(monkeypatch, listing, detail, seen, cms_titles)

    summary = run(session, [_product_source(listing, detail)])

    item = session.exec(select(ContentItem).where(ContentItem.source_url == PRODUCT)).one()
    assert summary.errors == 0
    assert summary.published == 0
    assert summary.ingested >= 1
    assert item.status == ContentStatus.PENDING_REVIEW
    assert "6 мм" in item.body_ru
    assert cms_titles == []

    published = publish_from_review(session, item.id, item.title_ru, item.body_ru)

    assert published.status == ContentStatus.PUBLISHED
    assert published.cms_post_id == 15
    assert cms_titles == [GLOBE_TITLE]
    assert collection.count() >= 1

    with _client(session) as client:
        response = client.post("/chat", json={"session_id": CHAT, "message": QUESTION})

    assert response.status_code == 200
    assert response.json() == {"answer": ANSWER, "escalate": False, "sources": [item.id]}
    assert QUESTION in seen["chat"]
    assert "6 мм" in seen["chat"]
    rows = session.exec(select(ChatMessage).order_by(ChatMessage.id)).all()
    assert [(row.role, row.text) for row in rows] == [
        ("user", QUESTION),
        ("assistant", ANSWER),
    ]


def _product_source(listing: str, detail: str) -> SourceSpec:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == LISTING:
            return httpx.Response(200, text=listing)
        if "pd535938348" in request.url.path:
            return httpx.Response(200, text=detail)
        return httpx.Response(404, text="missing")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return SourceSpec("products", lambda: fetch_products(LISTING, client=client))


def _mock_http(monkeypatch, listing: str, detail: str, seen: dict[str, str], cms_titles: list[str]):
    original = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/chat/completions"):
            return _completion(request, seen)
        if path.endswith("/embeddings"):
            return httpx.Response(
                200,
                json={"data": [{"embedding": [1.0, 0.0]}], "usage": {"prompt_tokens": 4}},
            )
        if path.startswith("/wp-json/wp/v2/posts"):
            payload = json.loads(request.content)
            cms_titles.append(payload["title"])
            return httpx.Response(200, json={"id": 15})
        if str(request.url) == LISTING:
            return httpx.Response(200, text=listing)
        if "pd535938348" in path:
            return httpx.Response(200, text=detail)
        return httpx.Response(404, text="missing")

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(llm_module.httpx, "Client", factory)
    monkeypatch.setattr(publish_module.httpx, "Client", factory)


def _completion(request: httpx.Request, seen: dict[str, str]) -> httpx.Response:
    payload = json.loads(request.content)
    system = payload["messages"][0]["content"]
    user = payload["messages"][-1]["content"]
    if "Не выдумывай цены" in system:
        seen["chat"] = user
        text = ANSWER
    else:
        text = _translation(user)
    body = {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 12},
    }
    return httpx.Response(200, json=body)


def _translation(user: str) -> str:
    title, body = _original_parts(user)
    original_len = max(len(title) + len(body), 1)
    if "Ultra-Precision Globe-Shaped Gyro Stabilizer" in title:
        text = _fit(GLOBE_SENTENCE, original_len, GLOBE_TITLE)
        return json.dumps({"title_ru": GLOBE_TITLE, "body_ru": text}, ensure_ascii=False)
    text = _fit(CARD_SENTENCE, original_len, CARD_TITLE)
    return json.dumps({"title_ru": CARD_TITLE, "body_ru": text}, ensure_ascii=False)


def _original_parts(user: str) -> tuple[str, str]:
    title = user.split("Заголовок:\n", 1)[-1].split("\n", 1)[0]
    body = user.split("Текст:\n", 1)[-1]
    return title, body


def _fit(sentence: str, original_len: int, title: str) -> str:
    room = max(int(original_len * 0.9) - len(title), len(sentence))
    text = sentence * max(1, room // len(sentence))
    upper = max(int(original_len * 2.4) - len(title), 1)
    return text[:upper].strip()


def _collection(monkeypatch):
    client = chromadb.EphemeralClient()
    collection = client.get_or_create_collection("content", metadata={"hnsw:space": "cosine"})
    monkeypatch.setattr("services.index.get_collection", lambda settings=None: collection)
    return collection


@contextmanager
def _client(session):
    def override():
        yield session

    app.dependency_overrides[get_session] = override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
