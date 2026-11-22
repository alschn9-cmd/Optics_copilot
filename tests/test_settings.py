from app.settings import Settings


def test_ingest_pages_are_read_from_env(monkeypatch):
    monkeypatch.setenv("NEWS_SOURCE_URL", "https://news.example/a/, https://news.example/b/")
    monkeypatch.setenv("PRODUCTS_SOURCE_URL", "https://shop.example/products.html")
    monkeypatch.setenv("PDF_URLS", "https://files.example/a.pdf, https://files.example/b.pdf")

    settings = Settings(_env_file=None)

    assert settings.news_source_list == [
        "https://news.example/a/",
        "https://news.example/b/",
    ]
    assert settings.product_source_list == ["https://shop.example/products.html"]
    assert settings.pdf_url_list == [
        "https://files.example/a.pdf",
        "https://files.example/b.pdf",
    ]
