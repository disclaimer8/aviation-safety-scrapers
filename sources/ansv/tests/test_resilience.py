"""ANSV lost pages and reports to failures it never reported.

Three separate leaks, all of them silent: a non-200 listing page was skipped,
page 1 was read without raise_for_status (so a 502 body parsed as a one-page
listing), and a failed report page was written down as pdf_url=None — a
transient fault recorded as the permanent fact "this report has no PDF".
"""
import pytest

from ansv_ingest import ansv, db
from ansv_ingest.pipeline import discover
from tests.conftest import FakeClient, FakeResp

from .test_pipeline import LISTING_URL, REPORT_URL, _LISTING_RESP, _REPORT_RESP


def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(ansv, "DELAY", 0)


class TestAFailedListingPageMustFailTheRun:
    def test_a_non_200_later_page_is_raised_at_the_end(self, monkeypatch):
        monkeypatch.setattr(ansv, "last_page", lambda html: 2)
        client = FakeClient({
            LISTING_URL: _LISTING_RESP,
            ansv.page_url(2): FakeResp(status_code=502),
            REPORT_URL: _REPORT_RESP,
        })
        with pytest.raises(RuntimeError, match="page 2: HTTP 502"):
            discover(_conn(), client)

    def test_a_502_on_page_1_is_raised_not_parsed_as_a_one_page_listing(self):
        # Without raise_for_status the error body parsed as a listing with no
        # entries and last_page()==1, so the walk "completed" over one page.
        client = FakeClient({LISTING_URL: FakeResp(status_code=502)})
        with pytest.raises(RuntimeError, match="HTTP 502"):
            discover(_conn(), client)

    def test_an_empty_page_1_is_a_markup_change(self, monkeypatch):
        monkeypatch.setattr(ansv, "last_page", lambda html: 1)
        client = FakeClient({LISTING_URL: FakeResp(text="<html>redesign</html>")})
        with pytest.raises(RuntimeError, match="page 1 yielded 0 entries"):
            discover(_conn(), client)


class TestATransientReportFailureIsNotAMissingPDF:
    def test_a_502_on_a_report_page_does_not_insert_a_pdf_less_row(self, monkeypatch):
        monkeypatch.setattr(ansv, "last_page", lambda html: 1)
        conn = _conn()
        client = FakeClient({
            LISTING_URL: _LISTING_RESP,
            REPORT_URL: FakeResp(status_code=502),
        })
        with pytest.raises(RuntimeError, match="i-colk"):
            discover(conn, client)

        # Nothing inserted: the next cycle re-discovers the report rather than
        # keeping a row that claims the report has no PDF.
        n = conn.execute("SELECT COUNT(*) c FROM ansv_reports").fetchone()["c"]
        assert n == 0

    def test_a_404_still_keeps_the_listing_row(self, monkeypatch):
        # A 404 is an answer, not a fault: the report page is gone, and the
        # listing metadata is worth keeping.
        monkeypatch.setattr(ansv, "last_page", lambda html: 1)
        conn = _conn()
        client = FakeClient({
            LISTING_URL: _LISTING_RESP,
            REPORT_URL: FakeResp(status_code=404),
        })
        assert discover(conn, client) == 1
        row = conn.execute("SELECT pdf_url FROM ansv_reports").fetchone()
        assert row["pdf_url"] is None
