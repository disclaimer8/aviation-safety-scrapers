# tests/test_bea.py
"""Offline tests for bea_ingest.bea using saved HTML fixtures."""
import os
import types

import pytest

from bea_ingest import bea

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


# ──────────────────────────────────────────────
# slug_from_detail_url
# ──────────────────────────────────────────────

def test_slug_from_detail_url():
    url = "/en/investigation-reports/notified-events/detail/accident-to-the-cessna-208-registered-f-hfdz-on-24-05-2026-at-fretoy-le-chateau-ad/"
    result = bea.slug_from_detail_url(url)
    assert result == "accident-to-the-cessna-208-registered-f-hfdz-on-24-05-2026-at-fretoy-le-chateau-ad"


def test_slug_from_detail_url_empty():
    assert bea.slug_from_detail_url("") == ""
    assert bea.slug_from_detail_url("/some/other/path/") == ""


# ──────────────────────────────────────────────
# parse_rows
# ──────────────────────────────────────────────

def test_parse_rows_landing():
    html = _fixture("landing.html")
    rows = bea.parse_rows(html)
    assert len(rows) >= 1
    for row in rows:
        assert "/detail/" in row["detail_url"], f"no /detail/ in {row['detail_url']!r}"
        assert row["title"], f"empty title for {row['detail_url']!r}"


def test_parse_rows_count_landing():
    html = _fixture("landing.html")
    rows = bea.parse_rows(html)
    # landing shows 10 per page
    assert len(rows) == 10


def test_parse_rows_global_page2():
    html = _fixture("global-page2.html")
    rows = bea.parse_rows(html)
    # global page 2 also shows 10 per page
    assert len(rows) == 10
    for row in rows:
        assert "/detail/" in row["detail_url"]
        assert row["title"]


# ──────────────────────────────────────────────
# year_facet_links
# ──────────────────────────────────────────────

def test_year_facet_links_landing():
    html = _fixture("landing.html")
    links = bea.year_facet_links(html)
    assert len(links) >= 1
    for url in links:
        # URL is percent-encoded: facetValue%5D= OR html-unescaped facetValue=
        assert "facetValue" in url, f"missing facetValue in {url!r}"
        assert "cHash=" in url, f"missing cHash in {url!r}"
        assert url.startswith("https://bea.aero"), f"not absolute: {url!r}"


# ──────────────────────────────────────────────
# next_page_link(html, current_page)
# New signature: current_page is an int; returns the page=(current+1) link or None
# ──────────────────────────────────────────────

def test_next_page_link_returns_page2_from_page1():
    """year-result.html is page 1 context; current_page=1 → should return page=2 link."""
    html = _fixture("year-result.html")
    url = bea.next_page_link(html, 1)
    assert url is not None
    assert "tx_news_pi1%5Bpage%5D=2" in url
    assert "cHash=" in url


def test_next_page_link_returns_page3_from_page2():
    """global-page2.html has pages 2,3,4,5; current_page=2 → returns page=3 link."""
    html = _fixture("global-page2.html")
    url = bea.next_page_link(html, 2)
    assert url is not None
    assert "tx_news_pi1%5Bpage%5D=3" in url
    assert "cHash=" in url


def test_next_page_link_returns_page5_from_page4():
    """year-result.html has pages up to 5; current_page=4 → returns page=5 link."""
    html = _fixture("year-result.html")
    url = bea.next_page_link(html, 4)
    assert url is not None
    assert "tx_news_pi1%5Bpage%5D=5" in url


def test_next_page_link_returns_none_at_last_page():
    """year-result.html has pages 2-5; current_page=5 → page=6 absent → None."""
    html = _fixture("year-result.html")
    url = bea.next_page_link(html, 5)
    assert url is None


def test_next_page_link_returns_none_on_empty_html():
    """A page with no paginator at all → None."""
    html = "<html><body><p>No paginator here</p></body></html>"
    url = bea.next_page_link(html, 1)
    assert url is None


def test_next_page_link_landing_has_page2():
    """Landing page fixture also contains a paginator → page=2 link found."""
    html = _fixture("landing.html")
    url = bea.next_page_link(html, 1)
    assert url is not None
    assert "tx_news_pi1%5Bpage%5D=2" in url


# ──────────────────────────────────────────────
# iter_events global walk (fake client)
# ──────────────────────────────────────────────

class _FakeResp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class _FakePaginatedClient:
    """
    Simulates the global pagination model:
    - GET LANDING → landing.html (10 rows, no paginator)
    - GET any year-facet URL → year-result.html (10 rows, paginator showing pages 2-5)
    - GET page=N URL for N in [2,3,4,5] → global-page2.html (10 rows, paginator showing pages 2-5)
    - GET page=N URL for N >= 6 → empty page (0 rows, no paginator) = stop
    """
    def __init__(self, fixtures):
        self._fixtures = fixtures  # dict: pattern -> html
        self.fetched_urls = []

    def get(self, url):
        self.fetched_urls.append(url)
        for pattern, html in self._fixtures.items():
            if pattern in url:
                return _FakeResp(html)
        return _FakeResp("<html><body>empty</body></html>")


def _load_fixtures():
    landing = open(os.path.join(FIXTURES, "landing.html"), encoding="utf-8").read()
    year_result = open(os.path.join(FIXTURES, "year-result.html"), encoding="utf-8").read()
    global_page2 = open(os.path.join(FIXTURES, "global-page2.html"), encoding="utf-8").read()
    return landing, year_result, global_page2


def test_iter_events_global_walk_deduplicates():
    """
    iter_events should walk global pages starting from landing and de-duplicate slugs.
    Landing page (page 1, has paginator) → pages 2, 3, 4, 5 via chain.
    No slug should appear twice; year-facet links should NOT be followed.
    """
    landing, year_result, global_page2 = _load_fixtures()

    class _TestClient:
        def __init__(self):
            self.fetched_urls = []
        def get(self, url):
            self.fetched_urls.append(url)
            if "tx_news_pi1%5Bpage%5D=" in url:
                return _FakeResp(global_page2)
            # landing (and anything else)
            return _FakeResp(landing)

    client = _TestClient()
    events = list(bea.iter_events(client, _max_pages=5))

    slugs = [e["slug"] for e in events]
    assert len(slugs) == len(set(slugs)), "Duplicate slugs found"
    assert len(slugs) >= 10, f"Expected >=10 events, got {len(slugs)}"

    # Verify page chain was followed via paginator
    page_fetches = [u for u in client.fetched_urls if "tx_news_pi1%5Bpage%5D=" in u]
    assert len(page_fetches) >= 1, "No paginator pages were followed"

    # Verify year-facet links were NOT followed
    facet_fetches = [u for u in client.fetched_urls if "facetTitle%5D=year_intS" in u]
    assert len(facet_fetches) == 0, f"Year facet links were incorrectly followed: {facet_fetches}"


# ──────────────────────────────────────────────
# get_detail_pdf_url — fake client
# ──────────────────────────────────────────────

class _FakeClient:
    def __init__(self, text):
        self._text = text
        self.called_urls = []

    def get(self, url):
        self.called_urls.append(url)
        return _FakeResp(self._text)


def test_get_detail_pdf_url():
    html = _fixture("detail.html")
    client = _FakeClient(html)
    pdf_url = bea.get_detail_pdf_url(client, "/en/investigation-reports/notified-events/detail/accident-to-the-microlight-identified-59dpk-on-28-12-2019-at-valenciennes/")
    assert pdf_url is not None
    assert pdf_url.startswith("https://bea.aero")
    assert "/fileadmin/" in pdf_url
    assert pdf_url.endswith(".pdf")


def test_get_detail_pdf_url_no_pdf():
    # a page with no PDF link should return None
    client = _FakeClient("<html><body>no pdf here</body></html>")
    result = bea.get_detail_pdf_url(client, "/en/investigation-reports/notified-events/detail/some-event/")
    assert result is None
