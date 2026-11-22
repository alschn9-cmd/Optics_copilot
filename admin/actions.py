import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlmodel import Session, select

from app.models import (
    AuditLog,
    ChatMessage,
    ContentItem,
    ContentStatus,
    PipelineMode,
    Setting,
    TokenUsage,
    advance_status,
)
from app.settings import get_settings
from pipeline import PIPELINE_MODE_KEY, current_pipeline_mode
from services.index import delete_item, upsert_item
from services.publish import CMSClient

_LOG_LINE = re.compile(r"^\d{4}-\d{2}-\d{2} ")
_RUN_MARK = "pipeline run:"


def review_queue(session: Session) -> list[ContentItem]:
    items = list(
        session.exec(
            select(ContentItem)
            .where(ContentItem.status == ContentStatus.PENDING_REVIEW)
            .order_by(ContentItem.updated_at.desc())
        ).all()
    )
    return items


def publish_from_review(
    session: Session,
    item_id: int,
    title_ru: str,
    body_ru: str,
    cms: CMSClient | None = None,
) -> ContentItem:
    item = _pending_item(session, item_id)
    item.title_ru = title_ru.strip()
    item.body_ru = body_ru.strip()
    session.add(item)
    session.commit()
    session.refresh(item)
    client = cms or CMSClient(session)
    client.create_or_update_post(item, mode="manual", actor="admin")
    upsert_item(session, item)
    session.refresh(item)
    return item


def reject_from_review(session: Session, item_id: int) -> ContentItem:
    item = _pending_item(session, item_id)
    advance_status(session, item, ContentStatus.REJECTED, "manual", "admin")
    session.commit()
    delete_item(item.id)
    session.refresh(item)
    return item


def set_pipeline_mode(session: Session, mode: PipelineMode | str) -> PipelineMode:
    chosen = mode if isinstance(mode, PipelineMode) else PipelineMode(mode)
    row = session.get(Setting, PIPELINE_MODE_KEY)
    if row is None:
        row = Setting(key=PIPELINE_MODE_KEY, value=chosen.value)
    else:
        row.value = chosen.value
    session.add(row)
    session.commit()
    return chosen


def get_pipeline_mode(session: Session) -> PipelineMode:
    return current_pipeline_mode(session)


def funnel(session: Session, start: date, end: date) -> list[tuple[str, int]]:
    begin, until = _period(start, end)
    fetched = sum(
        1 for item in session.exec(select(ContentItem)).all() if _in(item.created_at, begin, until)
    )
    logs = [
        row for row in session.exec(select(AuditLog)).all() if _in(row.created_at, begin, until)
    ]

    def count(status: ContentStatus) -> int:
        return sum(1 for row in logs if row.new_status == status)

    return [
        ("Забрано", fetched),
        ("Переведено", count(ContentStatus.TRANSLATED)),
        ("На ревью", count(ContentStatus.PENDING_REVIEW)),
        ("Опубликовано", count(ContentStatus.PUBLISHED)),
    ]


def usage_report(session: Session, start: date, end: date) -> dict:
    begin, until = _period(start, end)
    rows = [
        row for row in session.exec(select(TokenUsage)).all() if _in(row.created_at, begin, until)
    ]
    by_day: dict[str, float] = {}
    by_purpose: dict[str, float] = {}
    for row in rows:
        moment = _aware(row.created_at)
        day = moment.date().isoformat()
        by_day[day] = by_day.get(day, 0.0) + row.cost_usd
        purpose = row.purpose.value if hasattr(row.purpose, "value") else str(row.purpose)
        by_purpose[purpose] = by_purpose.get(purpose, 0.0) + row.cost_usd
    return {
        "total": sum(row.cost_usd for row in rows),
        "by_day": sorted(by_day.items()),
        "by_purpose": by_purpose,
    }


def bot_metrics(session: Session, start: date, end: date) -> dict:
    begin, until = _period(start, end)
    rows = [
        row for row in session.exec(select(ChatMessage)).all() if _in(row.created_at, begin, until)
    ]
    messages = len(rows)
    escalated = sum(1 for row in rows if row.escalated)
    return {
        "dialogs": len({row.session_id for row in rows}),
        "messages": messages,
        "escalated_share": (escalated / messages) if messages else 0.0,
    }


def recent_audit(session: Session, limit: int = 50) -> list[AuditLog]:
    return list(session.exec(select(AuditLog).order_by(AuditLog.id.desc())).all())[:limit]


def last_run_errors(log_text: str) -> str:
    if _RUN_MARK in log_text:
        head, _, _tail = log_text.rpartition(_RUN_MARK)
        _previous, separator, current = head.rpartition(_RUN_MARK)
        segment = current if separator else head
    else:
        segment = log_text
    kept: list[str] = []
    capturing = False
    for line in segment.splitlines():
        if " ERROR " in line:
            capturing = True
            kept.append(line)
            continue
        if _LOG_LINE.match(line):
            capturing = False
            continue
        if capturing:
            kept.append(line)
    return "\n".join(kept).strip()


def read_last_run_errors(path: Path | None = None) -> str:
    log_path = path or Path(get_settings().pipeline_log_path)
    if not log_path.is_file():
        return ""
    return last_run_errors(log_path.read_text(encoding="utf-8"))


def _pending_item(session: Session, item_id: int) -> ContentItem:
    item = session.get(ContentItem, item_id)
    if item is None:
        raise ValueError(f"item {item_id} was not found")
    if item.status != ContentStatus.PENDING_REVIEW:
        raise ValueError(f"item {item_id} is not pending review")
    return item


def _period(start: date, end: date) -> tuple[datetime, datetime]:
    begin = datetime.combine(start, datetime.min.time(), tzinfo=UTC)
    until = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    return begin, until


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment


def _in(moment: datetime, begin: datetime, until: datetime) -> bool:
    current = _aware(moment)
    return begin <= current < until
