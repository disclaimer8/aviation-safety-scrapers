"""A page CENIPA failed to serve must not be swallowed by the walk.

discover() had two swallowing layers: a `continue` on a page fetch error and a
catch-all around the whole per-page body. Between them, a run that lost pages
to 5xx returned a count and looked like a clean crawl.
"""
import pytest

from cenipa_ingest import cenipa, db
from cenipa_ingest.pipeline import discover

from .test_pipeline import FakeBrowser, _LISTING_HTML_PAGE1


def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(cenipa, "DELAY", 0)


class _BrowserDyingOnPage(FakeBrowser):
    def __init__(self, dead_page, pages=None):
        super().__init__(pages=pages)
        self._dead = dead_page

    def get_listing_html(self, n):
        if n == self._dead:
            raise RuntimeError("HTTP 502")
        return super().get_listing_html(n)


class TestAFailedPageMustFailTheRun:
    def test_a_502_mid_walk_is_raised_at_the_end(self):
        pages = {1: _LISTING_HTML_PAGE1, 3: _LISTING_HTML_PAGE1}
        with pytest.raises(RuntimeError, match="listing page"):
            discover(_conn(), _BrowserDyingOnPage(2, pages), max_pages=3)

    def test_rows_from_the_pages_that_worked_are_kept(self):
        pages = {1: _LISTING_HTML_PAGE1, 3: _LISTING_HTML_PAGE1}
        conn = _conn()
        with pytest.raises(RuntimeError):
            discover(conn, _BrowserDyingOnPage(2, pages), max_pages=3)
        n = conn.execute("SELECT COUNT(*) c FROM cenipa_reports").fetchone()["c"]
        assert n == 2

    def test_an_empty_page_1_is_a_markup_change_not_an_empty_source(self):
        # Guarded before the loop on purpose: raised inside it, the per-page
        # catch-all would swallow the error and keep walking.
        with pytest.raises(RuntimeError, match="page 1 yielded 0 rows"):
            discover(_conn(), FakeBrowser(pages={1: "<html><body>redesign</body></html>"}),
                     max_pages=3)

    def test_an_empty_later_page_still_ends_the_walk_quietly(self):
        # The legitimate end of the listing must stay legitimate.
        assert discover(_conn(), FakeBrowser(), max_pages=5) == 2
