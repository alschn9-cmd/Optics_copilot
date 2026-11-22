import logging
import re
from dataclasses import dataclass

import chromadb
from sqlmodel import Session

from app.models import ContentItem
from app.settings import Settings, get_settings
from services.llm import LLMClient

logger = logging.getLogger(__name__)

COLLECTION = "content"
CHUNK_TOKENS = 500
CHUNK_OVERLAP = 50
_TOKEN = re.compile(r"\S+")


@dataclass(frozen=True)
class Chunk:
    item_id: int
    index: int
    text: str
    title_ru: str
    source_type: str
    url: str

    @property
    def key(self) -> str:
        return f"{self.item_id}:{self.index}"

    def metadata(self) -> dict[str, str | int]:
        return {
            "item_id": self.item_id,
            "title_ru": self.title_ru,
            "source_type": self.source_type,
            "url": self.url,
        }


def chunk_item(item: ContentItem, settings: Settings | None = None) -> list[Chunk]:
    if item.id is None:
        raise ValueError("item has no id")
    tokens = _TOKEN.findall(item.body_ru or "")
    if not tokens:
        return []
    step = CHUNK_TOKENS - CHUNK_OVERLAP
    if step < 1:
        raise ValueError("chunk overlap must be smaller than the chunk size")
    chosen = settings or get_settings()
    url = _site_url(item, chosen)
    source_type = getattr(item.source_type, "value", item.source_type)
    chunks: list[Chunk] = []
    start = 0
    while start < len(tokens):
        piece = tokens[start : start + CHUNK_TOKENS]
        chunks.append(
            Chunk(
                item_id=item.id,
                index=len(chunks),
                text=" ".join(piece),
                title_ru=item.title_ru or "",
                source_type=source_type,
                url=url,
            )
        )
        if start + CHUNK_TOKENS >= len(tokens):
            break
        start += step
    return chunks


def upsert_item(
    session: Session,
    item: ContentItem,
    client: LLMClient | None = None,
) -> list[Chunk]:
    chunks = chunk_item(item)
    collection = get_collection()
    fresh = {chunk.key for chunk in chunks}
    stale = [key for key in _ids_for_item(collection, item.id) if key not in fresh]
    if stale:
        collection.delete(ids=stale)
    if not chunks:
        logger.info("removed index for item %s", item.id)
        return []
    llm = client or LLMClient(session)
    collection.upsert(
        ids=[chunk.key for chunk in chunks],
        embeddings=[llm.embed(chunk.text, item_id=item.id) for chunk in chunks],
        documents=[chunk.text for chunk in chunks],
        metadatas=[chunk.metadata() for chunk in chunks],
    )
    logger.info("indexed item %s chunks=%s", item.id, len(chunks))
    return chunks


def delete_item(item_id: int) -> None:
    collection = get_collection()
    ids = _ids_for_item(collection, item_id)
    if ids:
        collection.delete(ids=ids)
    logger.info("deleted index for item %s", item_id)


def index_item(session: Session, item: ContentItem) -> None:
    upsert_item(session, item)


def get_collection(settings: Settings | None = None):
    chosen = settings or get_settings()
    client = chromadb.PersistentClient(path=chosen.chroma_persist_dir)
    return client.get_or_create_collection(
        name=COLLECTION,
        metadata={"hnsw:space": "cosine"},
    )


def _site_url(item: ContentItem, settings: Settings) -> str:
    if item.cms_post_id:
        return f"{settings.wp_url.rstrip('/')}/?p={item.cms_post_id}"
    return item.source_url


def _ids_for_item(collection, item_id: int | None) -> list[str]:
    if item_id is None:
        return []
    found = collection.get(where={"item_id": item_id})
    return list(found.get("ids") or [])
