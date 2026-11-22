import hashlib
import logging
import re
from enum import StrEnum
from html import unescape

from sqlmodel import Session, select

from app.models import ContentItem, ContentStatus
from ingest.fetcher import FetchedItem

logger = logging.getLogger(__name__)

_WS = re.compile(r"\s+")


class StoreAction(StrEnum):
    CREATED = "created"
    SKIPPED = "skipped"
    UPDATED = "updated"


def normalize_text(value: str) -> str:
    return _WS.sub(" ", unescape(value or "")).strip().casefold()


def content_hash(title: str, body: str) -> str:
    payload = f"{normalize_text(title)}\n{normalize_text(body)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def store_fetched_item(session: Session, item: FetchedItem) -> StoreAction:
    """Insert a fetched item, or a new version when the normalized text changed.

    The caller commits. An unchanged hash for the same source_url is skipped.
    """
    digest = content_hash(item.title, item.body)
    existing = list(
        session.exec(select(ContentItem).where(ContentItem.source_url == item.source_url)).all()
    )
    if any(row.content_hash == digest for row in existing):
        logger.info("skip unchanged %s", item.source_url)
        return StoreAction.SKIPPED

    previous = max(existing, key=lambda row: (row.version, row.id or 0)) if existing else None
    session.add(
        ContentItem(
            source_url=item.source_url,
            source_type=item.source_type,
            title_original=item.title,
            body_original=item.body,
            status=ContentStatus.FETCHED,
            content_hash=digest,
            needs_ocr=item.needs_ocr,
            is_update=previous is not None,
            version=previous.version + 1 if previous is not None else 1,
            previous_item_id=previous.id if previous is not None else None,
        )
    )
    if previous is None:
        logger.info("queued new item %s", item.source_url)
        return StoreAction.CREATED
    logger.info("queued update of %s", item.source_url)
    return StoreAction.UPDATED
