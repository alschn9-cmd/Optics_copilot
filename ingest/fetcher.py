import json
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from html import unescape
from io import BytesIO
from urllib.parse import unquote, urljoin, urlsplit
from xml.etree import ElementTree

import httpx
from selectolax.lexbor import LexborHTMLParser

from app.models import SourceType
from app.settings import get_settings

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en"}
# Almost no extracted text means a scan. OCR is intentionally not done here.
OCR_MIN_CHARS = 40

_WS = re.compile(r"\s+")
_WP_FOOTER = re.compile(
    r"\s*The post .+ appeared first on Electronics Weekly\.?\s*$",
    re.IGNORECASE | re.DOTALL,
)
_CONTENT_ENCODED = "{http://purl.org/rss/1.0/modules/content/}encoded"


@dataclass(frozen=True)
class FetchedItem:
    source_url: str
    source_type: SourceType
    title: str
    body: str
    needs_ocr: bool = False


@contextmanager
def _borrow_client(client: httpx.Client | None) -> Iterator[httpx.Client]:
    if client is not None:
        yield client
        return
    with httpx.Client(headers=HEADERS, timeout=30.0, follow_redirects=True) as owned:
        yield owned


def fetch_news(
    page_url: str | None = None,
    client: httpx.Client | None = None,
) -> list[FetchedItem]:
    urls = [page_url] if page_url else get_settings().news_source_list
    if not urls:
        logger.warning("NEWS_SOURCE_URL is empty")
        return []
    documents: list[FetchedItem] = []
    with _borrow_client(client) as http:
        for url in urls:
            documents.extend(_fetch_news_page(http, url))
    return documents


def fetch_products(
    page_url: str | None = None,
    client: httpx.Client | None = None,
) -> list[FetchedItem]:
    urls = [page_url] if page_url else get_settings().product_source_list
    if not urls:
        logger.warning("PRODUCTS_SOURCE_URL is empty")
        return []
    documents: list[FetchedItem] = []
    with _borrow_client(client) as http:
        for url in urls:
            page = http.get(url)
            page.raise_for_status()
            found = parse_products_html(page.text, url)
            if not found:
                logger.warning("no product cards found at %s", url)
            documents.extend(_with_product_bodies(http, found))
    return documents


def fetch_pdf(url: str, client: httpx.Client | None = None) -> FetchedItem:
    with _borrow_client(client) as http:
        response = http.get(url)
        response.raise_for_status()
        payload = response.content
        if not payload.lstrip().startswith(b"%PDF"):
            raise ValueError(f"{url} did not return a PDF")
        return _item_from_pdf(url, payload)


def parse_rss(xml_text: str) -> list[FetchedItem]:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        logger.info("feed is not valid XML")
        return []
    if _local_name(root.tag) != "rss":
        return []

    documents: list[FetchedItem] = []
    seen: set[str] = set()
    for item in root.findall("./channel/item"):
        title = _xml_text(item.find("title"))
        link = _xml_text(item.find("link"))
        if not title or not link or link in seen:
            continue
        seen.add(link)
        raw_body = _xml_text(item.find(_CONTENT_ENCODED)) or _xml_text(item.find("description"))
        documents.append(
            FetchedItem(
                source_url=link,
                source_type=SourceType.NEWS,
                title=title,
                body=html_to_text(raw_body),
            )
        )
    return documents


def parse_news_html(html: str, page_url: str) -> list[FetchedItem]:
    tree = LexborHTMLParser(html)
    documents: list[FetchedItem] = []
    seen: set[str] = set()
    for article in tree.css("article.jeg_post"):
        linked = _news_link_and_title(article)
        if linked is None:
            continue
        href, title = linked
        url = urljoin(page_url, unescape(href))
        if url in seen:
            continue
        seen.add(url)
        excerpt = article.css_first(".jeg_post_excerpt p")
        body = excerpt.text(separator=" ", strip=True) if excerpt is not None else ""
        documents.append(
            FetchedItem(
                source_url=url,
                source_type=SourceType.NEWS,
                title=title,
                body=_collapse(body),
            )
        )
    return documents


def parse_news_article(html: str) -> str:
    tree = LexborHTMLParser(html)
    root = tree.css_first(".content-inner") or tree.css_first(".entry-content")
    if root is None:
        return ""
    for selector in ("script", "style", ".jeg_ad", ".jeg_post_tags", ".jeg_share_button"):
        for node in root.css(selector):
            node.decompose()
    return _collapse(root.text(separator="\n", strip=True))


def parse_product_page(html: str) -> str:
    tree = LexborHTMLParser(html)
    root = tree.css_first(".pro-this-prodBrief")
    if root is None:
        return ""
    for selector in ("script", "style"):
        for node in root.css(selector):
            node.decompose()
    return _collapse(root.text(separator=" ", strip=True))


def parse_products_html(html: str, page_url: str) -> list[FetchedItem]:
    cards = _product_cards(html, page_url)
    if cards:
        return cards
    return _products_from_jsonld(html, page_url)


def html_to_text(value: str) -> str:
    text = LexborHTMLParser(value or "").text(separator=" ", strip=True)
    text = unescape(_collapse(text))
    return _WP_FOOTER.sub("", text).strip()


def _fetch_news_page(client: httpx.Client, page_url: str) -> list[FetchedItem]:
    feed_url = urljoin(_as_directory(page_url), "feed/")
    documents = _items_from_feed(client, feed_url)
    if not documents:
        page = client.get(page_url)
        page.raise_for_status()
        for candidate in _feed_links(page.text, page_url):
            if candidate == feed_url:
                continue
            documents = _items_from_feed(client, candidate)
            if documents:
                break
        if not documents:
            logger.info("RSS unavailable for %s, parsing HTML list", page_url)
            documents = parse_news_html(page.text, page_url)
    if not documents:
        logger.warning("no news items from %s", page_url)
        return []
    return _with_article_bodies(client, documents)


def _with_product_bodies(client: httpx.Client, documents: list[FetchedItem]) -> list[FetchedItem]:
    filled: list[FetchedItem] = []
    for item in documents:
        page = _get_text(client, item.source_url)
        body = parse_product_page(page) if page else ""
        if not body:
            logger.warning("product description missing, keeping card text: %s", item.source_url)
            filled.append(item)
            continue
        filled.append(replace(item, body=body))
    return filled


def _with_article_bodies(client: httpx.Client, documents: list[FetchedItem]) -> list[FetchedItem]:
    filled: list[FetchedItem] = []
    for item in documents:
        page = _get_text(client, item.source_url)
        body = parse_news_article(page) if page else ""
        if not body:
            logger.warning("article body missing, keeping list excerpt: %s", item.source_url)
            filled.append(item)
            continue
        filled.append(replace(item, body=body))
    return filled


def _items_from_feed(client: httpx.Client, url: str) -> list[FetchedItem]:
    text = _get_text(client, url)
    if not text or not _looks_like_feed(text):
        return []
    return parse_rss(text)


def _item_from_pdf(url: str, payload: bytes) -> FetchedItem:
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(payload))
    parts = [page.extract_text() or "" for page in reader.pages]
    text = _collapse("\n".join(parts))
    filename = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    title = filename[:-4] if filename.lower().endswith(".pdf") else filename
    compact = re.sub(r"\s+", "", text)
    return FetchedItem(
        source_url=url,
        source_type=SourceType.PDF,
        title=title or url,
        body=text,
        needs_ocr=len(compact) < OCR_MIN_CHARS,
    )


def _product_cards(html: str, page_url: str) -> list[FetchedItem]:
    tree = LexborHTMLParser(html)
    documents: list[FetchedItem] = []
    seen: set[str] = set()
    for node in tree.css("a.prodlist-pro-name"):
        href = unescape((node.attributes.get("href") or "").strip())
        title = _collapse(node.attributes.get("title") or node.text(strip=True) or "")
        if not href or not title:
            continue
        url = urljoin(page_url, href)
        if url in seen:
            continue
        seen.add(url)
        description = _nearest_text(node, ".prodlist-list-des")
        documents.append(
            FetchedItem(
                source_url=url,
                source_type=SourceType.PRODUCT,
                title=title,
                body=description or title,
            )
        )
    return documents


def _products_from_jsonld(html: str, page_url: str) -> list[FetchedItem]:
    tree = LexborHTMLParser(html)
    documents: list[FetchedItem] = []
    seen: set[str] = set()
    for script in tree.css("script"):
        if (script.attributes.get("type") or "").lower() != "application/ld+json":
            continue
        raw = (script.text() or "").strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for node in _walk_json(payload):
            if node.get("@type") != "ItemList":
                continue
            for entry in node.get("itemListElement") or []:
                if not isinstance(entry, dict):
                    continue
                title = str(entry.get("name") or "").strip()
                href = str(entry.get("url") or "").strip()
                if not title or not href:
                    continue
                url = urljoin(page_url, href)
                if url in seen:
                    continue
                seen.add(url)
                image = entry.get("image")
                body = image.strip() if isinstance(image, str) and image.strip() else title
                documents.append(
                    FetchedItem(
                        source_url=url,
                        source_type=SourceType.PRODUCT,
                        title=title,
                        body=body,
                    )
                )
    return documents


def _nearest_text(node, selector: str) -> str:
    current = node.parent
    for _ in range(8):
        if current is None:
            return ""
        match = current.css_first(selector)
        if match is not None:
            return _collapse(match.text(separator=" ", strip=True))
        current = current.parent
    return ""


def _news_link_and_title(article) -> tuple[str, str] | None:
    title_link = article.css_first(".jeg_post_title a")
    if title_link is not None:
        href = title_link.attributes.get("href") or ""
        title = title_link.text(strip=True)
        if href and title:
            return href, unescape(title)
    for link in article.css("a"):
        label = link.attributes.get("aria-label") or ""
        prefix = "read article:"
        if not label.lower().startswith(prefix):
            continue
        href = link.attributes.get("href") or ""
        title = label.split(":", 1)[1].strip()
        if href and title:
            return href, unescape(title)
    return None


def _walk_json(payload) -> Iterator[dict]:
    if isinstance(payload, list):
        for item in payload:
            yield from _walk_json(item)
        return
    if not isinstance(payload, dict):
        return
    yield payload
    graph = payload.get("@graph")
    if isinstance(graph, list):
        for item in graph:
            yield from _walk_json(item)


def _get_text(client: httpx.Client, url: str) -> str | None:
    try:
        response = client.get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        response = getattr(exc, "response", None)
        status = response.status_code if response is not None else None
        logger.warning("request failed: %s status=%s", url, status, exc_info=True)
        return None
    return response.text


def _looks_like_feed(text: str) -> bool:
    sample = text.lstrip()[:300].lower()
    return sample.startswith("<?xml") or "<rss" in sample or "<feed" in sample


def _as_directory(url: str) -> str:
    return url if url.endswith("/") else f"{url}/"


def _feed_links(html: str, page_url: str) -> list[str]:
    tree = LexborHTMLParser(html)
    found: list[str] = []
    for link in tree.css("link"):
        rel = (link.attributes.get("rel") or "").lower()
        kind = (link.attributes.get("type") or "").lower()
        href = link.attributes.get("href")
        if "alternate" not in rel or not href:
            continue
        if "rss" not in kind and "atom" not in kind:
            continue
        url = urljoin(page_url, href)
        if "comment" in url:
            continue
        found.append(url)
    found.sort(key=lambda item: ("/news/" not in item, item))
    return found


def _xml_text(node: ElementTree.Element | None) -> str:
    if node is None or node.text is None:
        return ""
    return unescape(node.text).strip()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _collapse(value: str) -> str:
    return _WS.sub(" ", value).strip()
