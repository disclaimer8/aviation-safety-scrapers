"""Index parsing and report numbering. Fixtures are real URLs from the index."""
import pytest

from ntsbaar_ingest import db, ntsbaar, pipeline

R = "https://www.ntsb.gov/investigations/AccidentReports/Reports"


class TestReportNumbers:
    @pytest.mark.parametrize("url,number", [
        (f"{R}/AAR2101.pdf", "AAR2101"),
        (f"{R}/aar2103.pdf", "AAR2103"),      # the index carries both cases
        (f"{R}/AAR6908.pdf", "AAR6908"),
        (f"{R}/AAR7226.pdf", "AAR7226"),
    ])
    def test_it_reads_the_number_off_the_filename(self, url, number):
        assert ntsbaar.report_number(url) == number

    def test_a_revision_suffix_is_kept(self):
        # AAR7226A is a revision of AAR7226 and a separate document.
        assert ntsbaar.report_number(f"{R}/AAR7226A.pdf") == "AAR7226A"

    def test_a_non_report_pdf_yields_nothing(self):
        assert ntsbaar.report_number(f"{R}/annual_review_1998.pdf") is None
        assert ntsbaar.report_number("") is None
        assert ntsbaar.report_number(None) is None


class TestTheTwoDigitYear:
    @pytest.mark.parametrize("url,year", [
        (f"{R}/AAR6908.pdf", 1969),
        (f"{R}/AAR7226.pdf", 1972),
        (f"{R}/AAR9607.pdf", 1996),
        (f"{R}/AAR0101.pdf", 2001),
        (f"{R}/AAR2101.pdf", 2021),
    ])
    def test_it_pivots_on_the_collections_own_span(self, url, year):
        # 1967-2021, so 67 and above is the twentieth century.
        assert ntsbaar.adopted_year(url) == year

    def test_the_boundary_years_land_correctly(self):
        assert ntsbaar.adopted_year(f"{R}/AAR6701.pdf") == 1967
        assert ntsbaar.adopted_year(f"{R}/AAR6601.pdf") == 2066  # outside the span


class TestIndexParsing:
    def test_it_finds_the_reports(self):
        html = (f'<a href="{R}/AAR2101.pdf">2021-01</a>'
                f'<a href="{R}/AAR9607.pdf">1996-07</a>')
        rows = ntsbaar.parse_index(html)
        assert [r["case_id"] for r in rows] == ["AAR2101", "AAR9607"]
        assert rows[0]["adopted_year"] == 2021

    def test_the_same_report_in_two_letter_cases_is_one_row(self):
        # The index really does list AAR2103.pdf and aar2103.pdf.
        html = f'<a href="{R}/AAR2103.pdf">x</a><a href="{R}/aar2103.pdf">y</a>'
        assert len(ntsbaar.parse_index(html)) == 1

    def test_other_pdfs_on_the_page_are_ignored(self):
        html = (f'<a href="{R}/AAR2101.pdf">x</a>'
                '<a href="https://huntlibrary.erau.edu/style-guide.pdf">y</a>'
                f'<a href="{R}/safety_study_9401.pdf">z</a>')
        assert [r["case_id"] for r in ntsbaar.parse_index(html)] == ["AAR2101"]

    def test_an_empty_page_yields_nothing(self):
        assert ntsbaar.parse_index("") == []
        assert ntsbaar.parse_index("<html><body>nothing</body></html>") == []


class _Resp:
    def __init__(self, text="", content=b"", status=200):
        self.text = text
        self.content = content
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    def __init__(self, body):
        self.body = body

    def get(self, url, headers=None):
        return _Resp(text=self.body)


def _conn():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


class TestDiscover:
    def test_it_inserts_and_is_idempotent(self):
        html = f'<a href="{R}/AAR2101.pdf">x</a><a href="{R}/AAR9607.pdf">y</a>'
        conn = _conn()
        assert pipeline.discover(conn, _Client(html)) == 2
        assert pipeline.discover(conn, _Client(html)) == 0

    def test_an_index_that_yields_nothing_raises(self):
        # Discovery leans on a third party's page. If Hunt Library restyles it
        # or drops the NTSB links, a quiet zero would read as a clean run.
        with pytest.raises(RuntimeError, match="0 report links"):
            pipeline.discover(_conn(), _Client("<html><body>redesigned</body></html>"))


class TestBuild:
    def _parsed(self, conn, case_id, narrative):
        conn.execute(
            "INSERT INTO ntsbaar_reports (case_id, pdf_url, adopted_year, "
            "narrative_text, status, updated_at) VALUES (?,?,?,?,?,?)",
            (case_id, f"{R}/{case_id}.pdf", 1972, narrative, db.STATUS_PARSED, 0))
        conn.commit()

    def test_a_report_with_a_narrative_is_built(self):
        conn = _conn()
        self._parsed(conn, "AAR7226", "N" * 4000)
        assert pipeline.build(conn) == 1

    def test_a_scan_that_ocr_could_not_read_is_skipped(self):
        conn = _conn()
        self._parsed(conn, "AAR7402", "31 characters of page furniture")
        assert pipeline.build(conn) == 0
        assert conn.execute("SELECT status FROM ntsbaar_reports").fetchone()[0] \
               == db.STATUS_SKIPPED

    def test_the_adopted_year_is_never_used_as_the_event_date(self):
        # The Board adopts a report a year or two after the accident. Writing
        # 1972 as the event date for AAR7226 would misdate most of the
        # collection; the real date is in the prose.
        conn = _conn()
        self._parsed(conn, "AAR7226", "N" * 4000)
        pipeline.build(conn)
        row = conn.execute("SELECT event_date FROM ntsbaar_accidents").fetchone()
        assert row["event_date"] is None
