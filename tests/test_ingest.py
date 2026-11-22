import io

import httpx
import pytest
from pypdf import PdfWriter
from sqlmodel import select

from app.models import ContentItem, ContentStatus, SourceType, advance_status
from ingest.dedup import StoreAction, content_hash, store_fetched_item
from ingest.fetcher import (
    FetchedItem,
    fetch_news,
    fetch_pdf,
    fetch_products,
    parse_news_html,
    parse_products_html,
    parse_rss,
)
from ingest.run import SourceSpec, run_ingest

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <item>
      <title>Alpha &amp; Beta</title>
      <link>https://www.electronicsweekly.com/news/alpha/</link>
      <description><![CDATA[<p>Body   text</p>
      <p>The post Alpha appeared first on Electronics Weekly.</p>]]></description>
    </item>
    <item>
      <title>Second</title>
      <link>https://www.electronicsweekly.com/news/second/</link>
      <content:encoded><![CDATA[<p>Full article</p>]]></content:encoded>
      <description><![CDATA[<p>Short</p>]]></description>
    </item>
  </channel>
</rss>
"""

NEWS_HTML = """
<html><body>
  <article class="jeg_post jeg_pl_lg_2">
    <h2 class="jeg_post_title"><a href="/news/from-html/">From HTML</a></h2>
    <div class="jeg_post_excerpt"><p>Listing excerpt</p></div>
  </article>
  <article class="jeg_post">
    <h2 class="jeg_post_title"><a href="https://www.electronicsweekly.com/news/from-html/">Dup</a></h2>
  </article>
</body></html>
"""

PRODUCT_JSONLD = """
<html><head>
<script type="application/ld+json">
{"@graph":[
  {"@type":"BreadcrumbList","itemListElement":[
    {"@type":"ListItem","name":"Home","item":"https://www.ryanoptics.com/"}
  ]},
  {"@type":"ItemList","itemListElement":[
    {"@type":"ListItem","name":"Gyro One","url":"https://www.ryanoptics.com/Gyro-One-pd1.html",
     "image":"https://cdn.example/gyro.jpg"},
    {"@type":"ListItem","name":"Gyro Two","url":"https://www.ryanoptics.com/Gyro-Two-pd2.html"}
  ]}
]}
</script>
</head><body></body></html>
"""

PRODUCT_CARDS = """
<html><body>
<ul>
  <li>
    <a class="prodlist-pro-name" href="https://www.ryanoptics.com/Cam-pd9.html">Cam</a>
    <div class="prodlist-list-des">Range 8 km</div>
  </li>
</ul>
<script type="application/ld+json">
{"@type":"ItemList","itemListElement":[
  {"@type":"ListItem","name":"Ignored","url":"https://www.ryanoptics.com/Ignored-pd8.html"}
]}
</script>
</body></html>
"""


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _item(**overrides) -> FetchedItem:
    data = {
        "source_url": "https://example.com/news/1",
        "source_type": SourceType.NEWS,
        "title": "Title",
        "body": "Body text",
    }
    data.update(overrides)
    return FetchedItem(**data)


def test_parse_rss_prefers_encoded_content_and_strips_html():
    items = parse_rss(RSS)

    assert [item.title for item in items] == ["Alpha & Beta", "Second"]
    assert items[0].body == "Body text"
    assert items[1].body == "Full article"
    assert items[0].source_type is SourceType.NEWS


def test_parse_news_html_joins_relative_links_and_dedupes():
    items = parse_news_html(NEWS_HTML, "https://www.electronicsweekly.com/news/")

    assert len(items) == 1
    assert items[0].source_url == "https://www.electronicsweekly.com/news/from-html/"
    assert items[0].body == "Listing excerpt"


def test_fetch_news_uses_rss_before_html():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/feed/"):
            return httpx.Response(200, text=RSS)
        return httpx.Response(200, text=NEWS_HTML)

    with _client(handler) as client:
        items = fetch_news("https://www.electronicsweekly.com/news/", client=client)

    assert items[0].title == "Alpha & Beta"
    assert "From HTML" not in items[0].title


def test_fetch_news_loads_each_article_body():
    article = """
    <html><body>
      <div class="content-inner">
        <p>Full story about Alpha and the supply deal.</p>
        <script>GA_googleFillSlot("slot")</script>
        <div class="jeg_post_tags"><span>Tags:</span> AI</div>
      </div>
    </body></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/feed/"):
            return httpx.Response(200, text=RSS)
        if request.url.path.endswith("/alpha/"):
            return httpx.Response(200, text=article)
        return httpx.Response(200, text=NEWS_HTML)

    with _client(handler) as client:
        items = fetch_news("https://www.electronicsweekly.com/news/", client=client)

    assert items[0].title == "Alpha & Beta"
    assert items[0].body == "Full story about Alpha and the supply deal."
    assert "Tags:" not in items[0].body
    assert items[1].body == "Full article"


def test_fetch_news_falls_back_to_html_when_feed_is_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/feed/"):
            return httpx.Response(404, text="missing")
        return httpx.Response(200, text=NEWS_HTML)

    with _client(handler) as client:
        items = fetch_news("https://www.electronicsweekly.com/news/", client=client)

    assert [item.title for item in items] == ["From HTML"]


def test_parse_products_from_jsonld_skips_breadcrumbs():
    items = parse_products_html(PRODUCT_JSONLD, "https://www.ryanoptics.com/list.html")

    assert [item.title for item in items] == ["Gyro One", "Gyro Two"]
    assert items[0].source_type is SourceType.PRODUCT
    assert "gyro.jpg" in items[0].body
    assert items[1].body == "Gyro Two"


def test_parse_products_prefers_visible_cards():
    items = parse_products_html(PRODUCT_CARDS, "https://www.ryanoptics.com/list.html")

    assert len(items) == 1
    assert items[0].title == "Cam"
    assert items[0].body == "Range 8 km"


def test_fetch_products_requests_the_category_page():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text=PRODUCT_JSONLD)

    page = "https://www.ryanoptics.com/Marine-Gyro-Stabilized-Camera-pl44267786.html"
    with _client(handler) as client:
        items = fetch_products(page, client=client)

    assert len(items) == 2
    assert seen[0] == page
    assert set(seen[1:]) == {
        "https://www.ryanoptics.com/Gyro-One-pd1.html",
        "https://www.ryanoptics.com/Gyro-Two-pd2.html",
    }
    assert items[0].body == "https://cdn.example/gyro.jpg"


def test_fetch_products_replaces_card_text_with_detail_description():
    detail = """
    <html><body>
      <div class="pro-this-prodBrief">
        <p>Stable imaging at sea with a 1920x1080 sensor.</p>
        <script>track()</script>
      </div>
    </body></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/Cam-pd9.html"):
            return httpx.Response(200, text=detail)
        return httpx.Response(200, text=PRODUCT_CARDS)

    with _client(handler) as client:
        items = fetch_products("https://www.ryanoptics.com/list.html", client=client)

    assert len(items) == 1
    assert items[0].title == "Cam"
    assert items[0].body == "Stable imaging at sea with a 1920x1080 sensor."
    assert "track()" not in items[0].body


def test_fetch_pdf_extracts_text_and_flags_scans():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("scan.pdf"):
            return httpx.Response(200, content=_blank_pdf())
        return httpx.Response(
            200, content=_text_pdf("Marine gyro datasheet with enough extracted words")
        )

    with _client(handler) as client:
        readable = fetch_pdf("https://files.example/datasheet.pdf", client=client)
        scan = fetch_pdf("https://files.example/scan.pdf", client=client)

    assert readable.source_type is SourceType.PDF
    assert "datasheet" in readable.body
    assert readable.needs_ocr is False
    assert readable.title == "datasheet"
    assert scan.needs_ocr is True
    assert scan.body == ""


def test_fetch_pdf_rejects_non_pdf_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not a pdf</html>")

    with _client(handler) as client, pytest.raises(ValueError, match="did not return a PDF"):
        fetch_pdf("https://files.example/page.pdf", client=client)


def test_content_hash_ignores_case_and_whitespace():
    assert content_hash("Hello", "World  there") == content_hash(" hello ", "world there")


def test_store_skips_same_text_and_versions_changes(session):
    created = store_fetched_item(session, _item())
    session.commit()
    skipped = store_fetched_item(session, _item(body="  body   TEXT "))
    changed = store_fetched_item(session, _item(body="Edited body"))
    session.commit()

    rows = session.exec(select(ContentItem).order_by(ContentItem.version)).all()
    assert created is StoreAction.CREATED
    assert skipped is StoreAction.SKIPPED
    assert changed is StoreAction.UPDATED
    assert len(rows) == 2
    assert rows[0].version == 1
    assert rows[0].is_update is False
    assert rows[1].version == 2
    assert rows[1].is_update is True
    assert rows[1].status is ContentStatus.FETCHED
    assert rows[1].previous_item_id == rows[0].id
    assert rows[1].needs_ocr is False


def test_new_version_does_not_change_the_previous_status(session):
    store_fetched_item(session, _item())
    session.commit()
    original = session.exec(select(ContentItem)).one()
    advance_status(session, original, ContentStatus.TRANSLATED, "manual", "tester")
    session.commit()

    store_fetched_item(session, _item(body="Updated from source"))
    session.commit()
    session.refresh(original)
    latest = session.exec(select(ContentItem).order_by(ContentItem.version.desc())).first()

    assert original.status is ContentStatus.TRANSLATED
    assert latest.version == 2
    assert latest.status is ContentStatus.FETCHED


def test_run_keeps_going_when_one_source_fails(session):
    def broken() -> list[FetchedItem]:
        raise RuntimeError("site down")

    def healthy() -> list[FetchedItem]:
        return [_item(source_url="https://example.com/ok", title="Ok", body="Saved")]

    reports = run_ingest(
        session,
        [
            SourceSpec("news", broken),
            SourceSpec("products", healthy),
        ],
    )

    assert reports[0].error == "site down"
    assert reports[0].created == 0
    assert reports[1].created == 1
    assert reports[1].error is None
    stored = session.exec(select(ContentItem)).one()
    assert stored.source_url == "https://example.com/ok"
    assert stored.status is ContentStatus.FETCHED


def _text_pdf(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 50 100 Td ({escaped}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Count 1 /Kids [3 0 R] >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"
        ),
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    buffer = io.BytesIO()
    buffer.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(buffer.tell())
        buffer.write(f"{number} 0 obj\n".encode())
        buffer.write(body)
        buffer.write(b"\nendobj\n")
    xref = buffer.tell()
    total = len(objects) + 1
    buffer.write(f"xref\n0 {total}\n".encode())
    buffer.write(b"0000000000 65535 f \n")
    for offset in offsets:
        buffer.write(f"{offset:010d} 00000 n \n".encode())
    buffer.write(f"trailer << /Size {total} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    return buffer.getvalue()


def _blank_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
