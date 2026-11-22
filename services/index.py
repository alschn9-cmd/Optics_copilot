import logging

from sqlmodel import Session

from app.models import ContentItem

logger = logging.getLogger(__name__)


def index_item(session: Session, item: ContentItem) -> None:
    """Placeholder for the vector index. The session is unused until embeddings exist."""
    logger.info("index stub skipped for item %s", item.id)
