"""SACAA has exactly two listings — losing one silently loses half the source.

discover() logged the failure and `continue`d, so a 502 on the main listing
returned only the archive's rows and reported a clean run.
"""
import pytest

from sacaa_ingest import db, pipeline, sacaa

from .test_pipeline import FakeClient, FakeResp, _ARCHIVE, _MAIN


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(sacaa, "DELAY", 0)


class _ClientDyingOn(FakeClient):
    def __init__(self, dead_url, **kw):
        super().__init__(**kw)
        self._dead = dead_url

    def get(self, url, params=None):
        if url == self._dead:
            raise RuntimeError("HTTP 502")
        return super().get(url, params)


class TestALostListingMustFailTheRun:
    def test_a_failed_main_listing_is_raised(self, conn):
        with pytest.raises(RuntimeError, match="listing\\(s\\) failed"):
            pipeline.discover(conn, _ClientDyingOn(sacaa.MAIN_URL))

    def test_the_archive_rows_are_still_committed(self, conn):
        # Failing loudly must not throw away the half that worked.
        with pytest.raises(RuntimeError):
            pipeline.discover(conn, _ClientDyingOn(sacaa.MAIN_URL))
        ids = [r["case_id"] for r in conn.execute("SELECT case_id FROM sacaa_reports")]
        assert ids == ["5678"]

    def test_an_empty_main_listing_is_a_markup_change(self, conn):
        class _Redesigned(FakeClient):
            def get(self, url, params=None):
                if url == sacaa.MAIN_URL:
                    return FakeResp(text="<html><body>redesign</body></html>")
                return super().get(url, params)

        with pytest.raises(RuntimeError, match="yielded 0 rows"):
            pipeline.discover(conn, _Redesigned())

    def test_an_empty_archive_is_tolerated(self, conn):
        # Only the main listing is guarded: the archive page legitimately
        # renders empty while it is rebuilt.
        class _EmptyArchive(FakeClient):
            def get(self, url, params=None):
                if url == sacaa.ARCHIVE_URL:
                    return FakeResp(text="<table></table>")
                return super().get(url, params)

        assert pipeline.discover(conn, _EmptyArchive()) == 2

    def test_a_healthy_walk_is_unchanged(self, conn):
        assert pipeline.discover(conn, FakeClient()) == 3
