"""A dead hub or a failed year page must not report a clean, empty discover.

discover() used to `return 0` when the hub GET raised, and to `continue` past a
year page that failed. Both made a broken run indistinguishable from "AAIB
Malaysia published nothing this week" in the weekly timer's log.
"""
import pytest

from aaibmy_ingest import aaibmy, pipeline

from .test_pipeline import FakeClient, FakeResp


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(aaibmy, "DELAY", 0)


class TestDiscoverMustNotReportFailureAsEmptiness:
    def test_a_dead_hub_raises_instead_of_returning_zero(self, conn):
        class _DeadHub(FakeClient):
            def get(self, url, params=None):
                if url == aaibmy.HUB_URL:
                    raise RuntimeError("HTTP 502")
                return super().get(url, params)

        with pytest.raises(RuntimeError, match="hub failed after retries"):
            pipeline.discover(conn, _DeadHub())

    def test_a_hub_with_no_year_links_is_a_markup_change(self, conn):
        # The hub always lists years. Zero of them means the page changed, not
        # that the bureau retired its archive.
        with pytest.raises(RuntimeError, match="0 year links"):
            pipeline.discover(conn, FakeClient(hub="<html>redesigned</html>"))

    def test_a_failed_year_page_still_fails_the_run(self, conn):
        y2022 = ("https://www.mot.gov.my/en/aviation/reports/"
                 "statistics-and-accident-report-aaib/2022")

        class _DiesOn2022(FakeClient):
            def get(self, url, params=None):
                if url == y2022:
                    raise RuntimeError("HTTP 502")
                return super().get(url, params)

        with pytest.raises(RuntimeError, match="year page"):
            pipeline.discover(conn, _DiesOn2022())

    def test_the_other_years_are_still_walked_and_committed(self, conn):
        # Failing loudly must not cost the work that did succeed: the 2014 page
        # is walked even though 2022 failed, and its row survives the raise.
        y2022 = ("https://www.mot.gov.my/en/aviation/reports/"
                 "statistics-and-accident-report-aaib/2022")

        class _DiesOn2022(FakeClient):
            def get(self, url, params=None):
                if url == y2022:
                    raise RuntimeError("HTTP 502")
                return super().get(url, params)

        with pytest.raises(RuntimeError):
            pipeline.discover(conn, _DiesOn2022())

        n = conn.execute("SELECT COUNT(*) c FROM aaibmy_reports").fetchone()["c"]
        assert n == 1

    def test_a_healthy_walk_is_unchanged(self, conn):
        assert pipeline.discover(conn, FakeClient()) == 3
