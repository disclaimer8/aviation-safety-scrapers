"""Pipeline state-machine tests with a fake HTTP client (no network)."""
import httpx
import pytest

from rnsa_ingest import rnsa, pipeline

# A tiny 2013 page: an Icelandic final (ZZZ), a dual-reg final (DRO+KFB), an
# English final with ICAO+date (FIJ), an interim (MYX, no date), and a
# notification FORM that must be dropped.
_Y2013 = (
    '<section>'
    '<div class="item"><h3>TF-ZZZ</h3><p>Flugslys TF-ZZZ.</p>'
    '<a href="/media/1168/lokaskyrsla-tf-zzz-i-fluggordum-birk-thann-6-agust-2013.pdf">Skyrsla</a></div>'
    '<div class="item"><h3>TF-DRO og TF-KFB</h3><p>Flugumferdaratvik.</p>'
    '<a href="/media/1169/lokaskyrsla-um-flugumferdaratvik-tf-dro-og-tf-kfb-1.pdf">Skyrsla</a></div>'
    '<div class="item"><h3>TF-FIJ on BIKF</h3><p>Final report.</p>'
    '<a href="/media/1172/final-report-tf-fij-on-bikf-26-february-2013-1.pdf">Skyrsla</a></div>'
    '<div class="item"><h3>TF-MYX interim</h3><p>Bradabirgdaskyrsla.</p>'
    '<a href="/media/1170/bradabirgdaskyrsla-tf-myx.pdf">Skyrsla</a></div>'
    '<div class="item"><h3>Form</h3><p>Eydublad.</p>'
    '<a href="/media/9001/tilkynning-flugatvik-eydublad.pdf">Skyrsla</a></div>'
    '</section>'
)

_URL_2013 = "https://rnsa.is/flug/slysa-og-atvikaskyrslur/2013/"
_PDF_1168 = ("https://rnsa.is/media/1168/"
             "lokaskyrsla-tf-zzz-i-fluggordum-birk-thann-6-agust-2013.pdf")


class FakeResp:
    def __init__(self, text="", content=b"", status=200):
        self.text = text
        self.content = content
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "err", request=None, response=None)


class FakeClient:
    """Serves the 2013 page; every other year-page URL is a 404 (unpublished)."""

    def __init__(self, pages=None, pdfs=None, fail_pdf=None):
        self.pages = pages if pages is not None else {_URL_2013: _Y2013}
        self.pdfs = pdfs if pdfs is not None else {}
        self.fail_pdf = fail_pdf or set()
        self.requested = []

    def get(self, url, params=None):
        self.requested.append(url)
        if url in self.pages:
            return FakeResp(text=self.pages[url])
        if url in self.fail_pdf:
            raise RuntimeError("pdf 502")
        if url in self.pdfs:
            return FakeResp(content=self.pdfs[url])
        if "/media/" in url:
            return FakeResp(content=b"%PDF stub")
        # Any other year-page URL = 404 (future/unpublished year).
        return FakeResp(text="not found", status=404)


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(rnsa, "DELAY", 0)
    # Pin the walked range so the test is deterministic across calendar years.
    monkeypatch.setattr(rnsa, "FIRST_YEAR", 2013)
    monkeypatch.setattr(
        rnsa.datetime, "date",
        type("D", (), {"today": staticmethod(
            lambda: type("d", (), {"year": 2013})())}))


def test_discover_tolerates_404_and_filters_form(conn):
    n = pipeline.discover(conn, FakeClient())
    # 4 reports kept; the tilkynning form (9001) dropped.
    assert n == 4
    rows = {r["case_id"]: r for r in conn.execute("SELECT * FROM rnsa_reports")}
    assert set(rows) == {"1168", "1169", "1172", "1170"}
    assert rows["1168"]["registration"] == "TF-ZZZ"
    assert rows["1168"]["date_of_occurrence"] == "2013-08-06"
    assert rows["1168"]["report_kind"] == "Final"
    assert rows["1168"]["lang"] == "is"
    assert rows["1170"]["report_kind"] == "Interim"
    assert rows["1170"]["date_of_occurrence"] is None
    assert rows["1172"]["location"] == "BIKF"
    assert rows["1172"]["lang"] == "en"


def test_discover_walks_full_year_range_incl_future(conn):
    client = FakeClient()
    pipeline.discover(conn, client)
    # 2013 only (today pinned to 2013), probing 2013..2014 (current+1).
    assert _URL_2013 in client.requested
    assert "https://rnsa.is/flug/slysa-og-atvikaskyrslur/2014/" in \
        client.requested


def test_discover_idempotent(conn):
    assert pipeline.discover(conn, FakeClient()) == 4
    assert pipeline.discover(conn, FakeClient()) == 0


def test_fetch_pdf_tier_and_registration(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(
        pipeline.pdf, "extract_text",
        lambda p: "Skyrsla " + "N" * 9000 + " flugvel og flugmadur.")
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    rows = {r["case_id"]: r for r in conn.execute("SELECT * FROM rnsa_reports")}
    assert rows["1168"]["source_tier"] == "pdf"
    assert rows["1168"]["status"] == "parsed"
    # filename already carried TF-ZZZ; preserved.
    assert rows["1168"]["registration"] == "TF-ZZZ"


def test_fetch_registration_from_pdf_when_filename_lacks_it(conn, tmp_path,
                                                            monkeypatch):
    # Discover a report whose filename has no TF- mark (russian 97005).
    page = (
        '<section><div class="item"><h3>Russian aircraft</h3><p>x</p>'
        '<a href="/media/2672/m-01313-aig-09-russian-97005-final-report.pdf">'
        'Skyrsla</a></div></section>'
    )
    client = FakeClient(pages={_URL_2013: page})
    pipeline.discover(conn, client)
    row = conn.execute(
        "SELECT * FROM rnsa_reports WHERE case_id='2672'").fetchone()
    assert row["registration"] is None
    monkeypatch.setattr(
        pipeline.pdf, "extract_text",
        lambda p: "Aircraft TF-MYX " + "N" * 9000)
    pipeline.fetch(conn, FakeClient(pages={_URL_2013: page}),
                   pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT * FROM rnsa_reports WHERE case_id='2672'").fetchone()
    assert row["registration"] == "TF-MYX"  # recovered from PDF text


def test_fetch_scanned_tier(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "")
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT * FROM rnsa_reports WHERE case_id='1168'").fetchone()
    assert row["source_tier"] == "scanned"
    assert row["status"] == "parsed"


def test_fetch_failure_stays_new(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 9000)
    client = FakeClient(fail_pdf={_PDF_1168})
    pipeline.fetch(conn, client, pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT * FROM rnsa_reports WHERE case_id='1168'").fetchone()
    assert row["status"] == "new"  # retried next cycle


def test_build_floor_and_country(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())

    def _extract(p):
        return "N" * 9000 if "1168" in str(p) else "tiny"
    monkeypatch.setattr(pipeline.pdf, "extract_text", _extract)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    assert pipeline.build(conn) == 1
    acc = {r["case_id"]: r for r in conn.execute("SELECT * FROM rnsa_accidents")}
    assert set(acc) == {"1168"}
    assert acc["1168"]["country"] == "IS"
    assert acc["1168"]["report_type"] == "Final"
    assert acc["1168"]["event_date"] == "2013-08-06"
    assert acc["1168"]["lang"] == "is"
    assert acc["1168"]["source_url"].endswith("/2013/")
    skipped = conn.execute(
        "SELECT COUNT(*) c FROM rnsa_reports WHERE status='skipped'"
    ).fetchone()["c"]
    assert skipped == 3


def test_build_event_date_fallback(conn, tmp_path, monkeypatch):
    # MYX interim has no parseable filename date → falls back to {year}-01-01.
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 9000)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    pipeline.build(conn)
    acc = conn.execute(
        "SELECT event_date FROM rnsa_accidents WHERE case_id='1170'").fetchone()
    assert acc["event_date"] == "2013-01-01"


def test_build_idempotent(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 9000)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    pipeline.build(conn)
    conn.execute("UPDATE rnsa_reports SET status='parsed'")
    conn.commit()
    pipeline.build(conn)
    assert conn.execute(
        "SELECT COUNT(*) c FROM rnsa_accidents").fetchone()["c"] == 4
