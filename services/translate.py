import json
import logging
import re
from pathlib import Path

from sqlmodel import Session

from app.models import (
    ContentItem,
    ContentStatus,
    PipelineMode,
    SourceType,
    TokenPurpose,
    advance_status,
)
from app.settings import get_settings
from services.llm import LLMClient
from services.quality import find_anomalies

logger = logging.getLogger(__name__)

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
_PROMPT_FILES = {
    SourceType.NEWS: "translate_news.md",
    SourceType.PRODUCT: "translate_product.md",
    SourceType.PDF: "translate_product.md",
}
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def translate_item(
    session: Session,
    item: ContentItem,
    client: LLMClient | None = None,
    *,
    mode: PipelineMode | str | None = None,
    actor: str = "translator",
) -> ContentItem:
    if item.status != ContentStatus.FETCHED:
        raise ValueError(f"Cannot translate item in status {item.status}")

    llm = client or LLMClient(session)
    system = load_prompt(item.source_type)
    user = f"Заголовок:\n{item.title_original}\n\nТекст:\n{item.body_original}"
    raw = llm.complete(system, user, item_id=item.id, purpose=TokenPurpose.TRANSLATE)
    try:
        payload = parse_translation(raw)
    except json.JSONDecodeError:
        logger.info("translation JSON invalid for item %s, retrying once", item.id)
        raw = llm.complete(
            system,
            f"{user}\n\nПредыдущий ответ не был валидным JSON. Верни валидный JSON.",
            item_id=item.id,
            purpose=TokenPurpose.TRANSLATE,
        )
        payload = parse_translation(raw)

    item.title_ru = payload["title_ru"].strip()
    item.body_ru = payload["body_ru"].strip()
    reasons = find_anomalies(item)
    item.review_reason = "; ".join(reasons)
    target = ContentStatus.PENDING_REVIEW if reasons else ContentStatus.TRANSLATED
    chosen_mode = get_settings().pipeline_mode if mode is None else mode
    advance_status(session, item, target, chosen_mode, actor)
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def load_prompt(source_type: SourceType | str) -> str:
    kind = source_type if isinstance(source_type, SourceType) else SourceType(source_type)
    path = PROMPTS / _PROMPT_FILES[kind]
    return path.read_text(encoding="utf-8").strip()


def parse_translation(raw: str) -> dict[str, str]:
    text = _FENCE.sub("", raw.strip())
    data = json.loads(text)
    if not isinstance(data, dict) or "title_ru" not in data or "body_ru" not in data:
        raise json.JSONDecodeError("translation JSON must contain title_ru and body_ru", text, 0)
    return {"title_ru": str(data["title_ru"]), "body_ru": str(data["body_ru"])}
