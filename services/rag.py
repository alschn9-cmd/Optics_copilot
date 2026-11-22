import logging
from pathlib import Path

from sqlmodel import Session

import services.index as index_service
from app.models import TokenPurpose
from services.llm import LLMClient

logger = logging.getLogger(__name__)

TOP_K = 8
MIN_RELEVANCE = 0.35
PROMPT = Path(__file__).resolve().parents[1] / "prompts" / "support_bot.md"


def answer(
    question: str,
    history: list[dict] | None,
    session: Session,
    client: LLMClient | None = None,
) -> dict:
    llm = client or LLMClient(session)
    vector = llm.embed(question)
    hits = _relevant_chunks(vector)
    if not hits:
        logger.info("no relevant chunks for question")
        return {"answer": None, "escalate": True}

    text = llm.complete(
        PROMPT.read_text(encoding="utf-8").strip(),
        _user_message(question, history or [], hits),
        purpose=TokenPurpose.CHAT,
    )
    sources: list[int] = []
    for hit in hits:
        item_id = int(hit["item_id"])
        if item_id not in sources:
            sources.append(item_id)
    return {"answer": text, "sources": sources, "escalate": False}


def _relevant_chunks(vector: list[float]) -> list[dict]:
    collection = index_service.get_collection()
    count = collection.count()
    if count == 0:
        return []
    found = collection.query(
        query_embeddings=[vector],
        n_results=min(TOP_K, count),
        include=["documents", "metadatas", "distances"],
    )
    metadatas = (found.get("metadatas") or [[]])[0] or []
    documents = (found.get("documents") or [[]])[0] or []
    distances = (found.get("distances") or [[]])[0] or []
    hits: list[dict] = []
    for meta, document, distance in zip(metadatas, documents, distances):
        if not isinstance(meta, dict) or distance is None:
            continue
        if 1 - float(distance) < MIN_RELEVANCE:
            continue
        hits.append({**meta, "text": document or ""})
    return hits


def _user_message(question: str, history: list[dict], hits: list[dict]) -> str:
    turns = []
    for turn in history:
        role = str(turn.get("role") or "user")
        content = str(turn.get("content") or "")
        turns.append(f"{role}: {content}")
    context = []
    for hit in hits:
        context.append(f"[{hit['item_id']}] {hit.get('title_ru') or ''}\n{hit.get('text') or ''}")
    history_block = "\n".join(turns) if turns else "нет"
    context_block = "\n\n".join(context)
    return f"История:\n{history_block}\n\nКонтекст:\n{context_block}\n\nВопрос:\n{question}"
