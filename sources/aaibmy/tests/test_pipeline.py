"""Pipeline state-machine tests with a fake HTTP client (no network)."""
import pytest

from aaibmy_ingest import aaibmy, db, pipeline

_HUB = (
    '<a href="/en/aviation/reports/statistics-and-accident-report-aaib/2022">2022</a>'
    '<a href="/en/aviation/reports/statistics-and-accident-report-aaib/2014d">2014</a>'
)

_Y2022 = (
    '<a href="/en/AAIB%20Statistic%20%20Accident%20Report%20Document/2022/'
    '8.%20A%200822P%209M-SSW%20Final%20Report.pdf">a</a>'
    '<a href="/en/AAIB%20Statistic%20%20Accident%20Report%20Document/2022/'
    '2.%20SI%200222P%209M-MLS%20Final%20Report.pdf">b</a>'
    # Malay twin of the same report number — must be dropped.
    '<a href="/my/AAIBmy%20Statistik%20Kemalangan/2022/'
    'A%200822P%209M-SSW.pdf">my</a>'
)
_Y2014 = (
    '<a href="/en/AAIB%20Statistic%20%20Accident%20Report%20Document/2014/'
    '07%20July%202014.pdf">legacy</a>'
)

_PDF_SSW = ("https://www.mot.gov.my/en/AAIB%20Statistic%20%20Accident%20Report"
            "%20Document/2022/8.%20A%200822P%209M-SSW%20Final%20Report.pdf")
_PDF_MLS = ("https://www.mot.gov.my/en/AAIB%20Statistic%20%20Accident%20Report"
            "%20Document/2022/2.%20SI%200222P%209M-MLS%20Final%20Report.pdf")
_PDF_LEG = ("https://www.mot.gov.my/en/AAIB%20Statistic%20%20Accident%20Report"
            "%20Document/2014/07%20July%202014.pdf")


class FakeResp:
    def __init__(self, text="", content=b""):
        self.text = text
        self.content = content

    def raise_for_status(self):
        pass


class FakeClient:
    def __init__(self, hub=_HUB, pages=None, pdfs=None, fail_pdf=None):
        self.hub = hub
        self.pages = pages if pages is not None else {
            "https://www.mot.gov.my/en/aviation/reports/"
            "statistics-and-accident-report-aaib/2022": _Y2022,
            "https://www.mot.gov.my/en/aviation/reports/"
            "statistics-and-accident-report-aaib/2014d": _Y2014,
        }
        self.pdfs = pdfs if pdfs is not None else {
            _PDF_SSW: b"%PDF ssw", _PDF_MLS: b"%PDF mls", _PDF_LEG: b"%PDF leg"}
        self.fail_pdf = fail_pdf or set()
        self.requested = []

    def get(self, url, params=None):
        self.requested.append(url)
        if url == aaibmy.HUB_URL:
            return FakeResp(text=self.hub)
        if url in self.pages:
            return FakeResp(text=self.pages[url])
        if url in self.fail_pdf:
            raise RuntimeError("pdf 502")
        if url in self.pdfs:
            return FakeResp(content=self.pdfs[url])
        return FakeResp(text="")


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(aaibmy, "DELAY", 0)


def test_discover(conn):
    n = pipeline.discover(conn, FakeClient())
    # 2 EN reports in 2022 (Malay twin dropped) + 1 legacy 2014 = 3.
    assert n == 3
    rows = {r["case_id"]: r for r in conn.execute("SELECT * FROM aaibmy_reports")}
    assert set(rows) == {"a-08-22p", "si-02-22p", "07-july-2014"}
    assert rows["a-08-22p"]["registration"] == "9M-SSW"
    assert rows["a-08-22p"]["occurrence_type"] == "Accident"
    assert rows["si-02-22p"]["occurrence_type"] == "Serious Incident"
    assert rows["a-08-22p"]["year"] == "2022"
    assert rows["07-july-2014"]["year"] == "2014"  # 'd' stripped from stored year


def test_discover_idempotent(conn):
    assert pipeline.discover(conn, FakeClient()) == 3
    assert pipeline.discover(conn, FakeClient()) == 0


def test_fetch_pdf_tier(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 9000)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    rows = {r["case_id"]: r for r in conn.execute("SELECT * FROM aaibmy_reports")}
    assert rows["a-08-22p"]["source_tier"] == "pdf"
    assert len(rows["a-08-22p"]["narrative_text"]) == 9000
    assert rows["a-08-22p"]["status"] == "parsed"


def test_fetch_scanned_tier(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "")  # no text layer
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT * FROM aaibmy_reports WHERE case_id='a-08-22p'").fetchone()
    assert row["source_tier"] == "scanned"
    assert row["status"] == "parsed"


def test_fetch_failure_stays_new(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 9000)
    client = FakeClient(fail_pdf={_PDF_SSW})
    pipeline.fetch(conn, client, pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT * FROM aaibmy_reports WHERE case_id='a-08-22p'").fetchone()
    assert row["status"] == "new"  # download failed → retried next cycle


def test_build_floor(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    # SSW gets a long narrative; the rest stay short → skipped by the floor.
    def _extract(p):
        return "N" * 9000 if "a-08-22p" in str(p) else "tiny"
    monkeypatch.setattr(pipeline.pdf, "extract_text", _extract)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    assert pipeline.build(conn) == 1
    acc = {r["case_id"]: r for r in conn.execute("SELECT * FROM aaibmy_accidents")}
    assert set(acc) == {"a-08-22p"}
    assert acc["a-08-22p"]["country"] == "MY"
    assert acc["a-08-22p"]["registration"] == "9M-SSW"
    assert acc["a-08-22p"]["report_type"] == "Final"
    assert acc["a-08-22p"]["source_url"].endswith("/2022")
    # Short rows are marked skipped, not built.
    skipped = conn.execute(
        "SELECT COUNT(*) c FROM aaibmy_reports WHERE status='skipped'"
    ).fetchone()["c"]
    assert skipped == 2


def test_build_idempotent(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 9000)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    pipeline.build(conn)
    conn.execute("UPDATE aaibmy_reports SET status='parsed'")
    conn.commit()
    pipeline.build(conn)
    assert conn.execute(
        "SELECT COUNT(*) c FROM aaibmy_accidents").fetchone()["c"] == 3
