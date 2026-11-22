import chromadb

from app.models import ContentItem, SourceType
from services.index import upsert_item
from services.llm import LLMClient
from services.rag import answer

BODY = "Камера удерживает картинку на судне."
QUESTION = "как работает стабилизация"


def _collection(monkeypatch):
    client = chromadb.EphemeralClient()
    collection = client.get_or_create_collection("content", metadata={"hnsw:space": "cosine"})
    monkeypatch.setattr("services.index.get_collection", lambda: collection)
    return collection


def _item(session, body: str = BODY) -> ContentItem:
    item = ContentItem(
        source_url="https://source.example/gyro",
        source_type=SourceType.PRODUCT,
        title_original="Gyro",
        body_original="Gyro",
        title_ru="Гирокамера",
        body_ru=body,
        content_hash=body,
        cms_post_id=15,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def test_relevant_question_returns_answer_and_sources(session, monkeypatch):
    _collection(monkeypatch)
    item = _item(session)
    calls = {"complete": 0}

    def embed(self, text, model=None, *, item_id=None, purpose=None):
        return [1.0, 0.0]

    def complete(self, system, user, model=None, *, item_id=None, purpose=None):
        calls["complete"] += 1
        assert "Не выдумывай цены" in system
        assert QUESTION in user
        return "Камера удерживает картинку."

    monkeypatch.setattr(LLMClient, "embed", embed)
    monkeypatch.setattr(LLMClient, "complete", complete)
    upsert_item(session, item)

    result = answer(QUESTION, [], session)

    assert result == {
        "answer": "Камера удерживает картинку.",
        "sources": [item.id],
        "escalate": False,
    }
    assert calls["complete"] == 1


def test_irrelevant_question_escalates_without_llm(session, monkeypatch):
    _collection(monkeypatch)
    item = _item(session)

    def embed(self, text, model=None, *, item_id=None, purpose=None):
        if text == BODY:
            return [1.0, 0.0]
        return [0.0, 1.0]

    def complete(self, system, user, model=None, *, item_id=None, purpose=None):
        raise AssertionError("completion must not run without relevant chunks")

    monkeypatch.setattr(LLMClient, "embed", embed)
    monkeypatch.setattr(LLMClient, "complete", complete)
    upsert_item(session, item)

    result = answer("погода на марсе", [], session)

    assert result == {"answer": None, "escalate": True}
