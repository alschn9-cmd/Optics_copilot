import pytest
from sqlmodel import select

from app.models import (
    ALLOWED_TRANSITIONS,
    AuditLog,
    ContentItem,
    ContentStatus,
    PipelineMode,
    SourceType,
    advance_status,
)


def _item(**overrides) -> ContentItem:
    data = {
        "source_url": "https://example.com/news/1",
        "source_type": SourceType.NEWS,
        "title_original": "Title",
        "body_original": "Body",
        "content_hash": "abc123",
    }
    data.update(overrides)
    return ContentItem(**data)


def test_advance_status_writes_audit_log(session):
    item = _item()
    session.add(item)
    session.commit()
    session.refresh(item)

    old_updated_at = item.updated_at
    advance_status(session, item, ContentStatus.TRANSLATED, PipelineMode.MANUAL, "tester")
    session.commit()
    session.refresh(item)

    assert item.status == ContentStatus.TRANSLATED
    assert item.updated_at >= old_updated_at

    logs = session.exec(select(AuditLog)).all()
    assert len(logs) == 1
    log = logs[0]
    assert log.item_id == item.id
    assert log.old_status == ContentStatus.FETCHED
    assert log.new_status == ContentStatus.TRANSLATED
    assert log.mode == PipelineMode.MANUAL
    assert log.actor == "tester"
    assert log.action == "status_change"


def test_advance_status_rejects_illegal_transition(session):
    item = _item(status=ContentStatus.FETCHED)
    session.add(item)
    session.commit()
    session.refresh(item)

    with pytest.raises(ValueError, match="Cannot transition from fetched to published"):
        advance_status(session, item, ContentStatus.PUBLISHED, "auto", "tester")

    session.refresh(item)
    assert item.status == ContentStatus.FETCHED
    assert session.exec(select(AuditLog)).all() == []


def test_allowed_transitions_cover_pipeline():
    assert ContentStatus.TRANSLATED in ALLOWED_TRANSITIONS[ContentStatus.FETCHED]
    assert ContentStatus.PENDING_REVIEW in ALLOWED_TRANSITIONS[ContentStatus.FETCHED]
    assert ContentStatus.PUBLISHED in ALLOWED_TRANSITIONS[ContentStatus.TRANSLATED]
    assert ContentStatus.PUBLISHED in ALLOWED_TRANSITIONS[ContentStatus.PENDING_REVIEW]
    assert ALLOWED_TRANSITIONS[ContentStatus.PUBLISHED] == set()
