from pathlib import Path

from app.models import SourceType
from ingest.fetcher import (
    _item_from_pdf,
    parse_news_article,
    parse_news_html,
    parse_product_page,
    parse_products_html,
)

FIXTURES = Path(__file__).parent / "fixtures"
NEWS_PAGE = "https://www.electronicsweekly.com/news/"
PRODUCTS_PAGE = "https://www.ryanoptics.com/Marine-Gyro-Stabilized-Camera-pl44267786.html"
PRODUCT_PAGE = (
    "https://www.ryanoptics.com/Ultra-Precision-Globe-Shaped-Gyro-Stabilizer-pd535938348.html"
)


def test_parse_saved_news_page():
    html = (FIXTURES / "electronicsweekly_news.html").read_text(encoding="utf-8")
    items = parse_news_html(html, NEWS_PAGE)

    assert len(items) == 24
    assert len({item.source_url for item in items}) == 24
    assert items[0].title == "Where did the margin go? The hidden errors in PCB assembly"
    assert items[0].source_url == (
        "https://www.electronicsweekly.com/news/business/"
        "where-did-the-margin-go-the-hidden-errors-in-pcb-assembly-2026-11/"
    )
    assert all(item.source_type is SourceType.NEWS for item in items)
    assert all(item.source_url.startswith("https://www.electronicsweekly.com/") for item in items)
    google = next(item for item in items if item.title.startswith("Google"))
    assert "supply chain" in google.body


def test_parse_saved_news_article():
    html = (FIXTURES / "electronicsweekly_article.html").read_text(encoding="utf-8")
    body = parse_news_article(html)

    assert body.startswith("Google is rejigging the supply chain for its TPUs")
    assert "Dylan Patel told CNBC" in body
    assert "Ming-Chi Kuo" in body
    assert "Tags:" not in body
    assert "GA_googleFillSlot" not in body


def test_parse_saved_product_list():
    html = (FIXTURES / "ryanoptics_products.html").read_text(encoding="utf-8")
    items = parse_products_html(html, PRODUCTS_PAGE)

    assert len(items) == 12
    assert items[0].title == "Ultra-Precision Globe-Shaped Gyro Stabilizer"
    assert items[0].source_url == (
        "https://www.ryanoptics.com/Ultra-Precision-Globe-Shaped-Gyro-Stabilizer-pd535938348.html"
    )
    assert items[0].source_type is SourceType.PRODUCT
    assert "8km Long-range 1920×1080 HD U-Shaped Marine Gyro Stabilized EO/IR Camera" in {
        item.title for item in items
    }
    assert len({item.source_url for item in items}) == 12


def test_parse_saved_product_page():
    html = (FIXTURES / "ryanoptics_product.html").read_text(encoding="utf-8")
    body = parse_product_page(html)

    assert body.startswith("The Globe-shaped Gyro Stabilization System")
    assert "Focal length: 6mm" in body
    assert "Uncooled Vanadium Oxide" in body
    assert "offered by China manufacturer" not in body


def test_saved_product_page_is_not_parsed_as_a_list():
    html = (FIXTURES / "ryanoptics_product.html").read_text(encoding="utf-8")

    assert "Ultra-Precision Globe-Shaped Gyro Stabilizer from China manufacturer - Ryan" in html
    assert parse_products_html(html, PRODUCT_PAGE) == []


def test_parse_saved_pdf_extracts_text():
    payload = (FIXTURES / "sample.pdf").read_bytes()
    item = _item_from_pdf("https://pdfobject.com/pdf/sample.pdf", payload)

    assert payload.startswith(b"%PDF")
    assert item.source_type is SourceType.PDF
    assert item.title == "sample"
    assert item.needs_ocr is False
    assert "This is a simple PDF" in item.body
    assert len(item.body) > 40
