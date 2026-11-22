import chromadb
from sqlmodel import Session

from app.models import ContentItem, SourceType
from app.settings import Settings
from services.index import chunk_item, delete_item, upsert_item
from services.llm import LLMClient

SHOP = Settings(wp_url="https://shop.example", _env_file=None)


def _item(body: str, **overrides) -> ContentItem:
    data = {
        "id": 7,
        "source_url": "https://source.example/item",
        "source_type": SourceType.PRODUCT,
        "title_original": "Gyro",
        "body_original": "Gyro",
        "title_ru": "Гирокамера",
        "body_ru": body,
        "content_hash": "hash",
        "cms_post_id": 15,
    }
    data.update(overrides)
    return ContentItem(**data)


def test_chunk_boundaries_overlap_and_metadata():
    assert chunk_item(_item(""), SHOP) == []
    words = [f"w{i}" for i in range(500)]
    single = chunk_item(_item(" ".join(words)), SHOP)
    assert len(single) == 1
    assert single[0].text.split() == words
    assert single[0].key == "7:0"

    longer = [f"w{i}" for i in range(550)]
    chunks = chunk_item(_item(" ".join(longer)), SHOP)
    assert [chunk.text.split() for chunk in chunks] == [longer[:500], longer[450:]]
    assert [chunk.key for chunk in chunks] == ["7:0", "7:1"]
    assert chunks[0].metadata() == {
        "item_id": 7,
        "title_ru": "Гирокамера",
        "source_type": "product",
        "url": "https://shop.example/?p=15",
    }
    assert chunks[1].metadata() == chunks[0].metadata()


def _memory_collection(monkeypatch):
    client = chromadb.EphemeralClient()
    collection = client.get_or_create_collection("content", metadata={"hnsw:space": "cosine"})
    monkeypatch.setattr("services.index.get_collection", lambda: collection)
    return collection


def test_reindex_replaces_chunks_instead_of_duplicating(session: Session, monkeypatch):
    collection = _memory_collection(monkeypatch)
    words = [f"w{i}" for i in range(600)]
    item = _item(" ".join(words), id=None, content_hash="reindex")
    session.add(item)
    session.commit()
    session.refresh(item)

    def embed(self, text, model=None, *, item_id=None, purpose=None):
        return [1.0, 0.0]

    monkeypatch.setattr(LLMClient, "embed", embed)
    upsert_item(session, item)
    assert set(collection.get()["ids"]) == {f"{item.id}:0", f"{item.id}:1"}

    item.body_ru = "короткий текст"
    session.add(item)
    session.commit()
    upsert_item(session, item)
    assert set(collection.get()["ids"]) == {f"{item.id}:0"}
    assert collection.get()["documents"] == ["короткий текст"]

    delete_item(item.id)
    assert collection.get()["ids"] == []
