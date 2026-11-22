import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlmodel import Session

from app.db import engine, init_db
from app.settings import get_settings
from ingest.dedup import StoreAction, store_fetched_item
from ingest.fetcher import FetchedItem, fetch_news, fetch_pdf, fetch_products

logger = logging.getLogger(__name__)

SourceFetcher = Callable[[], list[FetchedItem]]


@dataclass(frozen=True)
class SourceSpec:
    name: str
    fetch: SourceFetcher


@dataclass
class SourceReport:
    name: str
    created: int = 0
    updated: int = 0
    skipped: int = 0
    error: str | None = None


def default_sources() -> list[SourceSpec]:
    settings = get_settings()
    sources: list[SourceSpec] = []
    for url in settings.news_source_list:
        sources.append(SourceSpec(f"news {url}", lambda url=url: fetch_news(url)))
    for url in settings.product_source_list:
        sources.append(SourceSpec(f"products {url}", lambda url=url: fetch_products(url)))
    for url in settings.pdf_url_list:
        sources.append(SourceSpec(f"pdf {url}", lambda url=url: [fetch_pdf(url)]))
    if not sources:
        logger.warning("NEWS_SOURCE_URL, PRODUCTS_SOURCE_URL and PDF_URLS are empty")
    return sources


def run_ingest(
    session: Session,
    sources: list[SourceSpec] | None = None,
) -> list[SourceReport]:
    reports: list[SourceReport] = []
    for source in default_sources() if sources is None else sources:
        reports.append(_run_source(session, source))
    return reports


def _run_source(session: Session, source: SourceSpec) -> SourceReport:
    try:
        documents = source.fetch()
    except Exception as exc:
        logger.exception("%s source failed", source.name)
        return SourceReport(name=source.name, error=str(exc))

    created = updated = skipped = 0
    for document in documents:
        try:
            action = store_fetched_item(session, document)
            session.commit()
        except Exception:
            logger.exception("failed to store %s", document.source_url)
            session.rollback()
            continue
        if action is StoreAction.CREATED:
            created += 1
        elif action is StoreAction.UPDATED:
            updated += 1
        else:
            skipped += 1
    logger.info(
        "%s: created=%s updated=%s skipped=%s",
        source.name,
        created,
        updated,
        skipped,
    )
    return SourceReport(
        name=source.name,
        created=created,
        updated=updated,
        skipped=skipped,
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    init_db()
    with Session(engine) as session:
        reports = run_ingest(session)
    logger.info("ingest finished: %s", reports)


if __name__ == "__main__":
    main()
