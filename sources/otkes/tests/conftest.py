# tests/conftest.py
import pytest

from otkes_ingest import db


# ─── DB fixture ───────────────────────────────────────────────────────────────

@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    yield c
    c.close()


# ─── Fake httpx client (PDF download) ─────────────────────────────────────────

class FakeResp:
    def __init__(self, *, content=b"", status_code=200):
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    """Maps a URL → FakeResp (or callable(url) -> FakeResp)."""

    def __init__(self, routes=None, default_content=b"%PDF-1.4 fake"):
        self.routes = routes or {}
        self.default_content = default_content
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        handler = self.routes.get(url)
        if handler is None:
            return FakeResp(content=self.default_content)
        return handler(url) if callable(handler) else handler

    def close(self):
        pass


@pytest.fixture
def make_client():
    return lambda **kw: FakeClient(**kw)


# ─── Fake browser (mirrors OtkesBrowser's public API, no Playwright) ──────────

class FakeBrowser:
    """Stand-in for OtkesBrowser.

    listings:   list of listing URLs harvest_listings() returns.
    year_pages: dict listing_url -> list of detail URLs.
    details:    dict detail_url -> metadata dict (get_detail() result).
    """

    def __init__(self, listings=None, year_pages=None, details=None):
        self.listings = listings or []
        self.year_pages = year_pages or {}
        self.details = details or {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def harvest_listings(self):
        return list(self.listings)

    def get_detail_urls(self, listing_url, year):
        return list(self.year_pages.get(listing_url, []))

    def get_detail(self, detail_url):
        return dict(self.details.get(detail_url, {}))


@pytest.fixture
def make_browser():
    return lambda **kw: FakeBrowser(**kw)
