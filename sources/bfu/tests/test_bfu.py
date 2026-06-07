# tests/test_bfu.py
"""Offline tests for bfu_ingest.bfu using the saved HTML fixture."""
import os
import time

import pytest

from bfu_ingest import bfu

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _fixture(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


# ──────────────────────────────────────────────────────────────────────────────
# parse_pdf_links
# ──────────────────────────────────────────────────────────────────────────────

class TestParsePdfLinks:
    def setup_method(self):
        self.html = _fixture("bfu-search.html")
        self.rows = bfu.parse_pdf_links(self.html)

    def test_returns_at_least_10_rows(self):
        assert len(self.rows) >= 10, f"Expected ≥10 rows, got {len(self.rows)}"

    def test_pdf_url_contains_untersuchungsberichte(self):
        for row in self.rows:
            assert "/Untersuchungsberichte/" in row["pdf_url"], (
                f"pdf_url missing /Untersuchungsberichte/: {row['pdf_url']!r}"
            )

    def test_pdf_url_ends_with_pdf(self):
        for row in self.rows:
            # URL may have query string after .pdf
            assert ".pdf" in row["pdf_url"], f".pdf missing in {row['pdf_url']!r}"

    def test_pdf_url_is_absolute(self):
        for row in self.rows:
            assert row["pdf_url"].startswith("https://"), (
                f"pdf_url not absolute: {row['pdf_url']!r}"
            )

    def test_filename_non_empty(self):
        for row in self.rows:
            assert row["filename"], f"empty filename for {row['pdf_url']!r}"

    def test_case_id_non_empty(self):
        for row in self.rows:
            assert row["case_id"], f"empty case_id for {row['pdf_url']!r}"

    def test_case_id_starts_with_BFU(self):
        for row in self.rows:
            assert row["case_id"].startswith("BFU"), (
                f"case_id does not start with BFU: {row['case_id']!r}"
            )

    def test_case_id_pattern(self):
        """case_id should look like BFU<YY>-<NNNN>-<N><X>"""
        import re
        pat = re.compile(r"^BFU\d{2}-\d{4}-\d[A-Z]$", re.IGNORECASE)
        for row in self.rows:
            assert pat.match(row["case_id"]), (
                f"case_id doesn't match pattern: {row['case_id']!r}"
            )

    def test_no_duplicate_pdf_urls(self):
        urls = [r["pdf_url"] for r in self.rows]
        assert len(urls) == len(set(urls)), "Duplicate pdf_urls found"

    def test_title_non_empty(self):
        for row in self.rows:
            assert row["title"].strip(), f"empty title for {row['pdf_url']!r}"

    def test_returns_dicts_with_expected_keys(self):
        for row in self.rows:
            assert set(row.keys()) == {"pdf_url", "filename", "case_id", "title"}, (
                f"unexpected keys: {row.keys()}"
            )


# ──────────────────────────────────────────────────────────────────────────────
# gtp_token
# ──────────────────────────────────────────────────────────────────────────────

class TestGtpToken:
    def setup_method(self):
        self.html = _fixture("bfu-search.html")

    def test_returns_non_empty_string(self):
        token = bfu.gtp_token(self.html)
        assert token is not None and token != "", "gtp_token returned None/empty"

    def test_token_is_numeric(self):
        token = bfu.gtp_token(self.html)
        assert token.isdigit(), f"gtp_token not numeric: {token!r}"

    def test_returns_none_on_no_pagination(self):
        result = bfu.gtp_token("<html><body>no pagination here</body></html>")
        assert result is None

    def test_fixture_token_is_675276(self):
        """The fixture uses the documented example token 675276."""
        token = bfu.gtp_token(self.html)
        assert token == "675276", f"Expected '675276', got {token!r}"


# ──────────────────────────────────────────────────────────────────────────────
# last_page
# ──────────────────────────────────────────────────────────────────────────────

class TestLastPage:
    def setup_method(self):
        self.html = _fixture("bfu-search.html")

    def test_returns_int(self):
        result = bfu.last_page(self.html)
        assert isinstance(result, int)

    def test_returns_at_least_2(self):
        result = bfu.last_page(self.html)
        assert result >= 2, f"Expected ≥2, got {result}"

    def test_fixture_has_119_pages(self):
        """The fixture encodes 119 as the last-page number."""
        result = bfu.last_page(self.html)
        assert result == 119, f"Expected 119, got {result}"

    def test_returns_1_on_no_pagination(self):
        result = bfu.last_page("<html><body>no pages</body></html>")
        assert result == 1


# ──────────────────────────────────────────────────────────────────────────────
# page_url
# ──────────────────────────────────────────────────────────────────────────────

class TestPageUrl:
    def test_contains_gtp(self):
        url = bfu.page_url("675276", 2)
        assert "gtp=" in url

    def test_contains_token(self):
        url = bfu.page_url("675276", 2)
        assert "675276" in url

    def test_contains_list(self):
        url = bfu.page_url("675276", 2)
        assert "list" in url.lower()

    def test_contains_page_number(self):
        url = bfu.page_url("675276", 2)
        assert "2" in url

    def test_page_3_contains_3(self):
        url = bfu.page_url("675276", 3)
        assert "3" in url

    def test_url_starts_with_search_base(self):
        url = bfu.page_url("675276", 2)
        assert url.startswith(bfu.SEARCH)

    def test_encoded_equals(self):
        """GSB pagination uses %3D (encoded =) in the list param."""
        url = bfu.page_url("675276", 2)
        assert "%3D" in url or "=" in url.split("gtp=", 1)[-1]


# ──────────────────────────────────────────────────────────────────────────────
# iter_reports with a fake client
# ──────────────────────────────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        pass


class _FakeClient:
    """Minimal fake: returns fixture HTML for page 1, and a page-2 variant for everything else."""

    def __init__(self, page1_html: str, page2_html: str = "", delay_check=False):
        self._page1 = page1_html
        self._page2 = page2_html or page1_html  # reuse page1 if not provided
        self.calls: list[str] = []

    def get(self, url: str):
        self.calls.append(url)
        if url == bfu.SEARCH:
            return _FakeResp(self._page1)
        return _FakeResp(self._page2)


def _make_page2_html(page1_html: str) -> str:
    """
    Build a page-2 fixture with different PDF filenames to avoid dedup killing all rows.
    Replace the year/aktenzeichen tokens so they look like a different page.
    """
    return page1_html.replace("2024", "2022").replace("2023", "2021").replace(
        "BFU24", "BFU22"
    ).replace("BFU23", "BFU21").replace("24-0", "22-0").replace("23-0", "21-0")


class TestIterReports:
    def setup_method(self):
        self.page1_html = _fixture("bfu-search.html")
        self.page2_html = _make_page2_html(self.page1_html)

    def test_yields_rows_from_page1(self):
        client = _FakeClient(self.page1_html, self.page2_html)
        rows = list(bfu.iter_reports(client, max_pages=1))
        assert len(rows) >= 10

    def test_each_row_has_required_keys(self):
        client = _FakeClient(self.page1_html)
        rows = list(bfu.iter_reports(client, max_pages=1))
        for row in rows:
            assert "pdf_url" in row
            assert "filename" in row
            assert "case_id" in row
            assert "title" in row

    def test_deduplicates_across_pages(self):
        """When page 2 returns the same HTML as page 1, all dupes are dropped."""
        client = _FakeClient(self.page1_html, page2_html=self.page1_html)
        rows = list(bfu.iter_reports(client, max_pages=2))
        urls = [r["pdf_url"] for r in rows]
        assert len(urls) == len(set(urls)), "Duplicate pdf_urls from iter_reports"

    def test_max_pages_1_fetches_only_search(self):
        client = _FakeClient(self.page1_html, self.page2_html)
        list(bfu.iter_reports(client, max_pages=1))
        assert client.calls == [bfu.SEARCH], (
            f"Expected only [SEARCH], got {client.calls}"
        )

    def test_max_pages_2_fetches_page2(self):
        client = _FakeClient(self.page1_html, self.page2_html)
        list(bfu.iter_reports(client, max_pages=2))
        assert len(client.calls) == 2
        assert client.calls[0] == bfu.SEARCH
        assert "gtp=" in client.calls[1]
        assert "list" in client.calls[1].lower()

    def test_yields_distinct_rows_from_two_different_pages(self):
        client = _FakeClient(self.page1_html, self.page2_html)
        rows = list(bfu.iter_reports(client, max_pages=2))
        # Page 1 has ≥10, page 2 has ≥10 different rows → expect ≥20 total
        assert len(rows) >= 20, f"Expected ≥20 rows across 2 pages, got {len(rows)}"

    def test_stops_when_no_pagination_token(self):
        """A page without gtp= pagination hrefs should yield rows then stop."""
        import re as _re
        # Remove the entire <nav class="pagination">…</nav> block so no gtp= hrefs remain
        bare_html = _re.sub(r'<nav[^>]*pagination[^>]*>.*?</nav>', '', self.page1_html, flags=_re.DOTALL)
        # Sanity: confirm gtp= is gone
        assert "gtp=" not in bare_html, "gtp= links still present after stripping nav"
        client = _FakeClient(bare_html)
        rows = list(bfu.iter_reports(client, max_pages=5))
        # Should have stopped at page 1
        assert client.calls == [bfu.SEARCH]
        assert len(rows) >= 10


class TestIterReportsClean:
    """Clean re-test of iter_reports without the max_pages_override mistake."""

    def setup_method(self):
        self.page1_html = _fixture("bfu-search.html")
        self.page2_html = _make_page2_html(self.page1_html)

    def test_yields_page1_rows_with_max_pages_1(self):
        client = _FakeClient(self.page1_html, self.page2_html)
        rows = list(bfu.iter_reports(client, max_pages=1))
        assert len(rows) >= 10


# ──────────────────────────────────────────────────────────────────────────────
# _stem_from_path and _case_id_from_stem (internal helpers, tested directly)
# ──────────────────────────────────────────────────────────────────────────────

def test_stem_from_path_strips_extension_and_query():
    stem = bfu._stem_from_path(
        "/DE/Publikationen/Untersuchungsberichte/2024/Bericht_24-0173-3X_Learjet35A_Rendsburg.pdf?__blob=publicationFile&v=4"
    )
    assert stem == "Bericht_24-0173-3X_Learjet35A_Rendsburg"


def test_stem_from_path_fbericht():
    stem = bfu._stem_from_path(
        "/DE/Publikationen/Untersuchungsberichte/2023/FBericht_23-0022-1X_Learjet35A_Rendsburg.pdf?__blob=publicationFile&v=6"
    )
    assert stem == "FBericht_23-0022-1X_Learjet35A_Rendsburg"


def test_case_id_from_stem_standard():
    cid = bfu._case_id_from_stem("Bericht_24-0173-3X_Learjet35A_Rendsburg")
    assert cid == "BFU24-0173-3X"


def test_case_id_from_stem_fbericht():
    cid = bfu._case_id_from_stem("FBericht_23-0022-1X_Learjet35A_Rendsburg")
    assert cid == "BFU23-0022-1X"


def test_case_id_from_stem_fallback():
    """If no aktenzeichen pattern is found, return the full stem."""
    cid = bfu._case_id_from_stem("SomethingUnparseable")
    assert cid == "SomethingUnparseable"
