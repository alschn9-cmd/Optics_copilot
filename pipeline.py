import argparse
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import ContentItem, ContentStatus, PipelineMode, Setting, advance_status
from app.settings import get_settings
from ingest.run import SourceSpec, run_ingest
from services.index import index_item
from services.llm import LLMClient
from services.publish import CMSClient
from services.translate import translate_item

logger = logging.getLogger(__name__)

PIPELINE_MODE_KEY = "pipeline_mode"


@dataclass
class RunSummary:
    ingested: int = 0
    translated: int = 0
    published: int = 0
    pending_review: int = 0
    errors: int = 0


def current_pipeline_mode(session: Session) -> PipelineMode:
    row = session.get(Setting, PIPELINE_MODE_KEY)
    raw = row.value if row is not None else get_settings().pipeline_mode
    return PipelineMode(raw)


def run(
    session: Session,
    sources: list[SourceSpec] | None = None,
) -> RunSummary:
    summary = RunSummary()
    mode = current_pipeline_mode(session)
    reports = run_ingest(session, sources)
    summary.ingested = sum(report.created + report.updated for report in reports)
    summary.errors = sum((1 if report.error else 0) + report.store_errors for report in reports)

    items = list(
        session.exec(select(ContentItem).where(ContentItem.status == ContentStatus.FETCHED)).all()
    )
    llm = LLMClient(session)
    cms = CMSClient(session)
    for item in items:
        try:
            translate_item(session, item, llm, mode=mode, actor="pipeline")
        except Exception:
            logger.exception("failed to translate item %s", item.id)
            session.rollback()
            summary.errors += 1
            continue

        summary.translated += 1
        if item.status == ContentStatus.PENDING_REVIEW:
            summary.pending_review += 1
            continue
        if mode is PipelineMode.MANUAL:
            advance_status(
                session,
                item,
                ContentStatus.PENDING_REVIEW,
                mode,
                "pipeline",
            )
            session.commit()
            summary.pending_review += 1
            continue

        try:
            cms.create_or_update_post(item, mode=mode, actor="pipeline")
        except Exception:
            logger.exception("failed to publish item %s", item.id)
            session.rollback()
            summary.errors += 1
            continue
        summary.published += 1
        try:
            index_item(session, item)
        except Exception:
            logger.exception("failed to index item %s", item.id)
            summary.errors += 1

    logger.info(
        "pipeline run: ingested=%s translated=%s published=%s pending_review=%s errors=%s",
        summary.ingested,
        summary.translated,
        summary.published,
        summary.pending_review,
        summary.errors,
    )
    return summary


def schedule() -> None:
    configure_logging()
    hours = get_settings().pipeline_interval_hours
    if hours < 1:
        raise ValueError("pipeline_interval_hours must be at least 1")
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        _run_once,
        "interval",
        hours=hours,
        next_run_time=datetime.now(UTC),
        id="pipeline",
    )
    logger.info("pipeline scheduler started, every %s hours", hours)
    scheduler.start()


def _run_once() -> None:
    init_db()
    with Session(engine) as session:
        run(session)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    path = Path(get_settings().pipeline_log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    resolved = str(path.resolve())
    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, logging.FileHandler) and handler.baseFilename == resolved:
            return
    file_handler = logging.FileHandler(resolved, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(file_handler)


def main(argv: list[str] | None = None) -> None:
    configure_logging()
    parser = argparse.ArgumentParser(prog="pipeline")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run")
    sub.add_parser("schedule")
    args = parser.parse_args(argv)
    if args.command == "schedule":
        schedule()
        return
    init_db()
    with Session(engine) as session:
        run(session)


if __name__ == "__main__":
    main()
