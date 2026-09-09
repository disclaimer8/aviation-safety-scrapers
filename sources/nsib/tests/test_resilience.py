"""A listing page that failed must not leave discover looking successful.

nsib's discover logged the exception and `continue`d, so a 502 on one of the
listing pages cost every report on it and the run still returned a count as if
the walk had been complete.
"""
import pytest

from nsib_ingest import db, nsib, pipeline

from .test_pipeline import _FAKE_ROWS, _FakeResp


def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


class _ClientDyingOn(object):
    """Serves the index; raises for one named page URL."""

    def __init__(self, dead_url):
        self.dead_url = dead_url
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        if url == self.dead_url:
            raise RuntimeError("HTTP 502")
        return _FakeResp("<index/>")


class TestAFailedPageMustFailTheRun:
    def test_a_502_on_a_later_page_is_raised_at_the_end(self, monkeypatch):
        monkeypatch.setattr(nsib, "iter_page_urls",
                            lambda html: [nsib.INDEX_URL, "https://nsib.gov.ng/page/2/"])
        monkeypatch.setattr(nsib, "parse_listing", lambda html: _FAKE_ROWS)
        monkeypatch.setattr(nsib, "fetch_api_rows", lambda client: [])
        monkeypatch.setattr(nsib, "DELAY", 0)

        with pytest.raises(RuntimeError, match="listing page"):
            pipeline.discover(_conn(), _ClientDyingOn("https://nsib.gov.ng/page/2/"),
                              wp_rest=False)

    def test_rows_from_the_pages_that_worked_are_kept(self, monkeypatch):
        monkeypatch.setattr(nsib, "iter_page_urls",
                            lambda html: [nsib.INDEX_URL, "https://nsib.gov.ng/page/2/"])
        monkeypatch.setattr(nsib, "parse_listing", lambda html: _FAKE_ROWS)
        monkeypatch.setattr(nsib, "fetch_api_rows", lambda client: [])
        monkeypatch.setattr(nsib, "DELAY", 0)
        conn = _conn()

        with pytest.raises(RuntimeError):
            pipeline.discover(conn, _ClientDyingOn("https://nsib.gov.ng/page/2/"),
                              wp_rest=False)

        # Page 1's three PDF-bearing rows are committed before the raise.
        n = conn.execute("SELECT COUNT(*) c FROM nsib_reports").fetchone()["c"]
        assert n == 3

    def test_a_complete_walk_still_returns_normally(self, monkeypatch):
        monkeypatch.setattr(nsib, "iter_page_urls", lambda html: [nsib.INDEX_URL])
        monkeypatch.setattr(nsib, "parse_listing", lambda html: _FAKE_ROWS)
        monkeypatch.setattr(nsib, "fetch_api_rows", lambda client: [])
        monkeypatch.setattr(nsib, "DELAY", 0)

        assert pipeline.discover(_conn(), _ClientDyingOn("nothing"), wp_rest=False) == 3
