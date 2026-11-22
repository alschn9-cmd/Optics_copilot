from datetime import UTC, datetime
from enum import StrEnum

from sqlmodel import Field, Session, SQLModel, UniqueConstraint


def utcnow() -> datetime:
    return datetime.now(UTC)


class SourceType(StrEnum):
    NEWS = "news"
    PRODUCT = "product"
    PDF = "pdf"


class ContentStatus(StrEnum):
    FETCHED = "fetched"
    TRANSLATED = "translated"
    PENDING_REVIEW = "pending_review"
    PUBLISHED = "published"
    REJECTED = "rejected"


class PipelineMode(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


class TokenPurpose(StrEnum):
    TRANSLATE = "translate"
    CHAT = "chat"
    EMBED = "embed"


ALLOWED_TRANSITIONS: dict[ContentStatus, set[ContentStatus]] = {
    ContentStatus.FETCHED: {
        ContentStatus.TRANSLATED,
        ContentStatus.PENDING_REVIEW,
        ContentStatus.REJECTED,
    },
    ContentStatus.TRANSLATED: {
        ContentStatus.PENDING_REVIEW,
        ContentStatus.PUBLISHED,
        ContentStatus.REJECTED,
    },
    ContentStatus.PENDING_REVIEW: {
        ContentStatus.PUBLISHED,
        ContentStatus.REJECTED,
        ContentStatus.TRANSLATED,
    },
    ContentStatus.PUBLISHED: set(),
    ContentStatus.REJECTED: {ContentStatus.PENDING_REVIEW},
}


class ContentItem(SQLModel, table=True):
    __tablename__ = "content_item"
    __table_args__ = (
        UniqueConstraint("source_url", "content_hash", name="uq_content_item_source_hash"),
    )

    id: int | None = Field(default=None, primary_key=True)
    source_url: str = Field(index=True)
    source_type: SourceType
    title_original: str
    body_original: str
    title_ru: str = ""
    body_ru: str = ""
    review_reason: str = ""
    status: ContentStatus = ContentStatus.FETCHED
    cms_post_id: int | None = None
    content_hash: str
    needs_ocr: bool = False
    is_update: bool = False
    version: int = 1
    previous_item_id: int | None = Field(default=None, foreign_key="content_item.id", index=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class AuditLog(SQLModel, table=True):
    __tablename__ = "audit_log"

    id: int | None = Field(default=None, primary_key=True)
    item_id: int = Field(foreign_key="content_item.id", index=True)
    action: str
    old_status: ContentStatus | None = None
    new_status: ContentStatus
    mode: PipelineMode
    actor: str
    created_at: datetime = Field(default_factory=utcnow)


class Setting(SQLModel, table=True):
    __tablename__ = "setting"

    key: str = Field(primary_key=True)
    value: str


class TokenUsage(SQLModel, table=True):
    __tablename__ = "token_usage"

    id: int | None = Field(default=None, primary_key=True)
    item_id: int | None = Field(default=None, foreign_key="content_item.id", index=True)
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    purpose: TokenPurpose
    created_at: datetime = Field(default_factory=utcnow)


def _as_status(value: ContentStatus | str) -> ContentStatus:
    return value if isinstance(value, ContentStatus) else ContentStatus(value)


def _as_mode(value: PipelineMode | str) -> PipelineMode:
    return value if isinstance(value, PipelineMode) else PipelineMode(value)


def advance_status(
    session: Session,
    item: ContentItem,
    new_status: ContentStatus | str,
    mode: PipelineMode | str,
    actor: str,
) -> ContentItem:
    target = _as_status(new_status)
    pipeline_mode = _as_mode(mode)
    current = _as_status(item.status)

    if target not in ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"Cannot transition from {current.value} to {target.value}")

    log = AuditLog(
        item_id=item.id,
        action="status_change",
        old_status=current,
        new_status=target,
        mode=pipeline_mode,
        actor=actor,
    )
    item.status = target
    item.updated_at = utcnow()
    session.add(item)
    session.add(log)
    return item
