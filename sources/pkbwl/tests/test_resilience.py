"""A transport failure mid-walk must not look like the end of the listing.

PKBWL's discover used to `break` on any exception from fetch_listing. A 404 is
a clean past-the-end stop; a 502 on page 30 is not, and stopping on it turned a
truncated crawl into a run that reported success with no new reports.
"""
import pytest

from pkbwl_ingest import pipeline, pkbwl

from .test_pipeline import FakeClient, FakeResp, _LISTING_P1


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    # test_pipeline.py's autouse fixture does not reach this module, and the
    # real 1.2s DELAY would make every walk here sleep for seconds.
    monkeypatch.setattr(pkbwl, "DELAY", 0)


class TestDiscoverMustNotMistakeFailureForTheEnd:
    def test_a_502_mid_walk_is_raised_not_swallowed(self, conn):
        class _DiesOnPage2(FakeClient):
            def get(self, url, params=None):
                if "page/2" in url:
                    raise RuntimeError("HTTP 502")
                return super().get(url, params)

        with pytest.raises(RuntimeError, match="page 2 failed after retries"):
            pipeline.discover(conn, _DiesOnPage2())

    def test_rows_found_before_the_failure_are_kept(self, conn):
        # Raising must not roll back real work — the next run resumes instead
        # of starting the walk over.
        class _DiesOnPage2(FakeClient):
            def get(self, url, params=None):
                if "page/2" in url:
                    raise RuntimeError("HTTP 502")
                return super().get(url, params)

        with pytest.raises(RuntimeError):
            pipeline.discover(conn, _DiesOnPage2())

        n = conn.execute("SELECT COUNT(*) c FROM pkbwl_reports").fetchone()["c"]
        assert n == 2

    def test_a_404_still_ends_the_walk_quietly(self, conn):
        # The legitimate past-the-end stop must stay legitimate.
        assert pipeline.discover(conn, FakeClient()) == 2

    def test_an_empty_first_page_is_an_error_not_an_empty_source(self, conn):
        # PKBWL publishes ~2,300 reports; zero slugs on page 1 means the
        # listing markup changed, which stop-on-empty read as "no reports".
        class _Redesigned(FakeClient):
            def get(self, url, params=None):
                if "page/" in url or url.rstrip("/").endswith("raporty"):
                    return FakeResp(text="<html><body>nothing we know</body></html>")
                return super().get(url, params)

        with pytest.raises(RuntimeError, match="page 1 yielded 0 report slugs"):
            pipeline.discover(conn, _Redesigned())
