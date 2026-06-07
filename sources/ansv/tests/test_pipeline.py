# tests/test_pipeline.py
"""
Pipeline tests for ANSV discover / fetch / parse / build.

All HTTP calls are monkeypatched; no real network or filesystem I/O needed.
"""
import os
import tempfile

import pytest

from ansv_ingest import ansv, db
from ansv_ingest.pipeline import discover, fetch, parse, build
from tests.conftest import FakeResp, FakeClient


# ── helpers ───────────────────────────────────────────────────────────────────

def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


# Minimal listing HTML with one entry linking to REPORT_URL
LISTING_URL = ansv.LISTING_URL
REPORT_URL = "https://ansv.it/i-colk/"
PDF_URL = "https://ansv.it/wp-content/uploads/Relazione-I-COLK.pdf"

_LISTING_HTML = f"""
<html>
<body>
<article class="card">
  <div class="card-text"><p>
    Incidente occorso all'elicottero AW139 marche I-COLK in data 16/03/2024 aeroporto di Roma
  </p></div>
  <h5 class="card-title">Roma, AW139</h5>
  <a href="{REPORT_URL}" class="read-more">Leggi di più</a>
</article>
</body>
</html>
"""

_REPORT_HTML = f"""
<html>
<body>
<article class="card">
  <h1 class="entry-title">Roma, I-COLK AW139, 16/03/2024</h1>
  <div class="entry-content">
    <p>RELAZIONE DI INCHIESTA</p>
    <p>Incidente occorso all'elicottero AW139 marche I-COLK in data 16/03/2024</p>
    <a href="{PDF_URL}">Relazione-I-COLK.pdf</a>
  </div>
</article>
</body>
</html>
"""

_LISTING_RESP = FakeResp(text=_LISTING_HTML)
_REPORT_RESP = FakeResp(text=_REPORT_HTML)
_PDF_BYTES = b"%PDF-1.4 fake"


def _make_client():
    return FakeClient({
        LISTING_URL: _LISTING_RESP,
        REPORT_URL: _REPORT_RESP,
        PDF_URL: FakeResp(content=_PDF_BYTES),
    })


# ── discover ──────────────────────────────────────────────────────────────────

def test_discover_inserts_row(monkeypatch):
    monkeypatch.setattr(ansv, "last_page", lambda html: 1)
    monkeypatch.setattr(ansv, "parse_listing", lambda html: [{
        "report_url": REPORT_URL,
        "title": "Roma, AW139",
        "aircraft": "AW139",
        "registration": "I-COLK",
        "date_of_occurrence": "2024-03-16",
        "location": "Roma",
    }])
    monkeypatch.setattr(ansv, "parse_report", lambda html: {
        "pdf_url": PDF_URL,
        "title": "Roma, I-COLK AW139",
    })
    monkeypatch.setattr(ansv, "DELAY", 0)

    conn = _conn()
    client = _make_client()
    n = discover(conn, client)

    assert n == 1
    row = conn.execute("SELECT * FROM ansv_reports WHERE case_id='I-COLK_2024-03-16'").fetchone()
    assert row is not None
    assert row["report_url"] == REPORT_URL
    assert row["pdf_url"] == PDF_URL
    assert row["aircraft"] == "AW139"
    assert row["registration"] == "I-COLK"
    assert row["date_of_occurrence"] == "2024-03-16"
    assert row["location"] == "Roma"
    assert row["status"] == db.STATUS_NEW


def test_discover_idempotent(monkeypatch):
    """Running discover twice inserts only 1 row total."""
    monkeypatch.setattr(ansv, "last_page", lambda html: 1)
    monkeypatch.setattr(ansv, "parse_listing", lambda html: [{
        "report_url": REPORT_URL,
        "title": "Roma, AW139",
        "aircraft": "AW139",
        "registration": "I-COLK",
        "date_of_occurrence": "2024-03-16",
        "location": "Roma",
    }])
    monkeypatch.setattr(ansv, "parse_report", lambda html: {
        "pdf_url": PDF_URL,
        "title": "Roma, I-COLK AW139",
    })
    monkeypatch.setattr(ansv, "DELAY", 0)

    conn = _conn()
    client = _make_client()
    n1 = discover(conn, client)
    n2 = discover(conn, client)

    assert n1 == 1
    assert n2 == 0
    total = conn.execute("SELECT COUNT(*) FROM ansv_reports").fetchone()[0]
    assert total == 1


def test_discover_bad_report_page_skips_gracefully(monkeypatch):
    """A 404 on the report page should not abort the run; row is still inserted with pdf_url=None."""
    monkeypatch.setattr(ansv, "last_page", lambda html: 1)
    monkeypatch.setattr(ansv, "parse_listing", lambda html: [{
        "report_url": "https://ansv.it/bad-report/",
        "title": "Bad",
        "aircraft": "C172",
        "registration": "I-FAIL",
        "date_of_occurrence": "2020-01-01",
        "location": "Somewhere",
    }])
    monkeypatch.setattr(ansv, "DELAY", 0)

    conn = _conn()
    # Client returns 404 for the bad report URL
    client = FakeClient({
        LISTING_URL: _LISTING_RESP,
        "https://ansv.it/bad-report/": FakeResp(status_code=404),
    })
    n = discover(conn, client)

    # Row is still inserted (with pdf_url=None)
    assert n == 1
    row = conn.execute("SELECT * FROM ansv_reports WHERE case_id='I-FAIL_2020-01-01'").fetchone()
    assert row is not None
    assert row["pdf_url"] is None


def test_discover_multi_page(monkeypatch):
    """discover walks all pages returned by last_page."""
    call_count = {"n": 0}

    def fake_parse_listing(html):
        call_count["n"] += 1
        return [{
            "report_url": f"{REPORT_URL}p{call_count['n']}/",
            "title": f"Entry {call_count['n']}",
            "aircraft": "C172",
            "registration": f"I-P{call_count['n']:02d}",
            "date_of_occurrence": f"2023-01-0{call_count['n']}",
            "location": "Milan",
        }]

    monkeypatch.setattr(ansv, "last_page", lambda html: 3)
    monkeypatch.setattr(ansv, "parse_listing", fake_parse_listing)
    monkeypatch.setattr(ansv, "parse_report", lambda html: {"pdf_url": None, "title": None})
    monkeypatch.setattr(ansv, "DELAY", 0)

    conn = _conn()
    # Client handles all page URLs
    routes = {ansv.page_url(i): FakeResp(text=f"page{i}") for i in range(1, 4)}
    for i in range(1, 4):
        routes[f"{REPORT_URL}p{i}/"] = FakeResp(text="<html></html>")
    client = FakeClient(routes)

    n = discover(conn, client)
    assert n == 3
    total = conn.execute("SELECT COUNT(*) FROM ansv_reports").fetchone()[0]
    assert total == 3


# ── fetch ─────────────────────────────────────────────────────────────────────

def _insert_new(conn, case_id="I-COLK_2024-03-16", pdf_url=PDF_URL):
    conn.execute(
        "INSERT INTO ansv_reports (case_id, report_url, pdf_url, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (case_id, REPORT_URL, pdf_url, db.STATUS_NEW, db.now_ms(), db.now_ms()),
    )
    conn.commit()


def test_fetch_downloads_pdf_and_advances(monkeypatch, tmp_path):
    monkeypatch.setattr(ansv, "DELAY", 0)

    def fake_download(client, url, dest):
        with open(dest, "wb") as f:
            f.write(_PDF_BYTES)

    monkeypatch.setattr(ansv, "download", fake_download)

    conn = _conn()
    _insert_new(conn)

    client = _make_client()
    n = fetch(conn, client, str(tmp_path))

    assert n == 1
    row = conn.execute("SELECT * FROM ansv_reports WHERE case_id='I-COLK_2024-03-16'").fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert row["pdf_path"] is not None
    assert row["pdf_path"].endswith(".pdf")
    assert os.path.basename(row["pdf_path"]).startswith("I-COLK")


def test_fetch_no_pdf_url_advances_with_none(monkeypatch, tmp_path):
    """A row without a pdf_url should be advanced to 'fetched' with pdf_path=None."""
    monkeypatch.setattr(ansv, "DELAY", 0)

    conn = _conn()
    _insert_new(conn, pdf_url=None)

    client = _make_client()
    n = fetch(conn, client, str(tmp_path))

    assert n == 1
    row = conn.execute("SELECT * FROM ansv_reports WHERE case_id='I-COLK_2024-03-16'").fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert row["pdf_path"] is None


def test_fetch_download_failure_stays_new(monkeypatch, tmp_path):
    """If download raises, the row stays at 'new' (not advanced)."""
    monkeypatch.setattr(ansv, "DELAY", 0)

    def bad_download(client, url, dest):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(ansv, "download", bad_download)

    conn = _conn()
    _insert_new(conn)

    client = _make_client()
    fetch(conn, client, str(tmp_path))

    row = conn.execute("SELECT * FROM ansv_reports WHERE case_id='I-COLK_2024-03-16'").fetchone()
    assert row["status"] == db.STATUS_NEW


# ── parse ─────────────────────────────────────────────────────────────────────

def _insert_fetched(conn, case_id, pdf_path=None):
    conn.execute(
        "INSERT INTO ansv_reports "
        "(case_id, report_url, status, pdf_path, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (case_id, REPORT_URL, db.STATUS_FETCHED, pdf_path, db.now_ms(), db.now_ms()),
    )
    conn.commit()


def test_parse_long_text_tier_pdf(monkeypatch, tmp_path):
    """A PDF with >=600 chars → source_tier='pdf'."""
    pdf_path = str(tmp_path / "test.pdf")
    with open(pdf_path, "wb") as f:
        f.write(b"%PDF fake")

    long_text = "A" * 700
    monkeypatch.setattr("ansv_ingest.pipeline.extract_text", lambda p: long_text)

    conn = _conn()
    _insert_fetched(conn, "I-COLK_2024-03-16", pdf_path=pdf_path)

    n = parse(conn)
    assert n == 1
    row = conn.execute("SELECT * FROM ansv_reports WHERE case_id='I-COLK_2024-03-16'").fetchone()
    assert row["status"] == db.STATUS_PARSED
    assert row["source_tier"] == "pdf"
    assert row["narrative_text"] == long_text


def test_parse_tiny_text_scanned(monkeypatch, tmp_path):
    """A PDF that yields tiny text (scanned image) → source_tier='scanned'."""
    pdf_path = str(tmp_path / "scanned.pdf")
    with open(pdf_path, "wb") as f:
        f.write(b"%PDF scanned")

    monkeypatch.setattr("ansv_ingest.pipeline.extract_text", lambda p: "tiny")

    conn = _conn()
    _insert_fetched(conn, "I-SCANNED_2001-01-01", pdf_path=pdf_path)

    n = parse(conn)
    assert n == 1
    row = conn.execute("SELECT * FROM ansv_reports WHERE case_id='I-SCANNED_2001-01-01'").fetchone()
    assert row["status"] == db.STATUS_PARSED
    assert row["source_tier"] == "scanned"


def test_parse_empty_text_scanned(monkeypatch, tmp_path):
    """A PDF that yields empty text (pure image scan) → source_tier='scanned'."""
    pdf_path = str(tmp_path / "image.pdf")
    with open(pdf_path, "wb") as f:
        f.write(b"%PDF image")

    monkeypatch.setattr("ansv_ingest.pipeline.extract_text", lambda p: "")

    conn = _conn()
    _insert_fetched(conn, "I-IMAGE_2005-05-05", pdf_path=pdf_path)

    parse(conn)
    row = conn.execute("SELECT * FROM ansv_reports WHERE case_id='I-IMAGE_2005-05-05'").fetchone()
    assert row["source_tier"] == "scanned"


def test_parse_no_pdf_path_tier_none(monkeypatch):
    """A row with no pdf_path → source_tier='none', narrative=''."""
    monkeypatch.setattr("ansv_ingest.pipeline.extract_text", lambda p: "should not be called")

    conn = _conn()
    _insert_fetched(conn, "I-NOPDF_2020-01-01", pdf_path=None)

    parse(conn)
    row = conn.execute("SELECT * FROM ansv_reports WHERE case_id='I-NOPDF_2020-01-01'").fetchone()
    assert row["status"] == db.STATUS_PARSED
    assert row["source_tier"] == "none"
    assert (row["narrative_text"] or "") == ""


# ── build ─────────────────────────────────────────────────────────────────────

def _insert_parsed(conn, case_id, narrative, source_tier="pdf",
                   aircraft="AW139", registration="I-COLK",
                   location="Roma", date_of_occurrence="2024-03-16",
                   pdf_url=PDF_URL, report_url=REPORT_URL):
    conn.execute(
        "INSERT INTO ansv_reports "
        "(case_id, report_url, pdf_url, status, narrative_text, source_tier, "
        "aircraft, registration, location, date_of_occurrence, "
        "discovered_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (case_id, report_url, pdf_url, db.STATUS_PARSED,
         narrative, source_tier, aircraft, registration,
         location, date_of_occurrence, db.now_ms(), db.now_ms()),
    )
    conn.commit()


def test_build_pdf_tier_creates_accident(monkeypatch):
    """A 'pdf' tier row with narrative >= 80 → ansv_accidents row with country='IT'."""
    conn = _conn()
    narrative = "X" * 200
    _insert_parsed(conn, "I-COLK_2024-03-16", narrative, source_tier="pdf")

    n = build(conn)
    assert n == 1

    acc = conn.execute(
        "SELECT * FROM ansv_accidents WHERE case_id='I-COLK_2024-03-16'"
    ).fetchone()
    assert acc is not None
    assert acc["country"] == "IT"
    assert acc["event_date"] == "2024-03-16"
    assert acc["narrative_text"] == narrative
    assert acc["source_url"] == PDF_URL

    rep = conn.execute(
        "SELECT status FROM ansv_reports WHERE case_id='I-COLK_2024-03-16'"
    ).fetchone()
    assert rep["status"] == db.STATUS_BUILT


def test_build_scanned_tier_skipped(monkeypatch):
    """A 'scanned' tier row → status='skipped', NOT inserted into ansv_accidents."""
    conn = _conn()
    _insert_parsed(conn, "I-SCAN_2001-01-01", narrative="", source_tier="scanned")

    n = build(conn)
    assert n == 0

    acc = conn.execute(
        "SELECT * FROM ansv_accidents WHERE case_id='I-SCAN_2001-01-01'"
    ).fetchone()
    assert acc is None

    rep = conn.execute(
        "SELECT status FROM ansv_reports WHERE case_id='I-SCAN_2001-01-01'"
    ).fetchone()
    assert rep["status"] == db.STATUS_SKIPPED


def test_build_none_tier_skipped():
    """A 'none' tier row (no PDF) → status='skipped'."""
    conn = _conn()
    _insert_parsed(conn, "I-NONE_2020-01-01", narrative="", source_tier="none")

    n = build(conn)
    assert n == 0

    rep = conn.execute(
        "SELECT status FROM ansv_reports WHERE case_id='I-NONE_2020-01-01'"
    ).fetchone()
    assert rep["status"] == db.STATUS_SKIPPED


def test_build_pdf_short_narrative_skipped():
    """A 'pdf' tier row with narrative < 80 chars → skipped."""
    conn = _conn()
    _insert_parsed(conn, "I-SHORT_2022-06-06", narrative="too short", source_tier="pdf")

    n = build(conn)
    assert n == 0

    rep = conn.execute(
        "SELECT status FROM ansv_reports WHERE case_id='I-SHORT_2022-06-06'"
    ).fetchone()
    assert rep["status"] == db.STATUS_SKIPPED


def test_build_source_url_falls_back_to_report_url():
    """When pdf_url is None, source_url in ansv_accidents = report_url."""
    conn = _conn()
    narrative = "Y" * 200
    _insert_parsed(
        conn, "I-NOPDF_2023-03-03", narrative, source_tier="pdf",
        pdf_url=None, report_url=REPORT_URL,
    )

    build(conn)
    acc = conn.execute(
        "SELECT source_url FROM ansv_accidents WHERE case_id='I-NOPDF_2023-03-03'"
    ).fetchone()
    assert acc["source_url"] == REPORT_URL


def test_build_mixed_rows():
    """One pdf + one scanned → 1 built, 1 skipped."""
    conn = _conn()
    _insert_parsed(conn, "I-PDF_2024-01-01", "Z" * 200, source_tier="pdf",
                   registration="I-PDF", location="Milan", aircraft="B738")
    _insert_parsed(conn, "I-SCAN_2024-01-02", "", source_tier="scanned",
                   registration="I-SCAN", location="Rome", aircraft="C172")

    n = build(conn)
    assert n == 1

    built_count = conn.execute("SELECT COUNT(*) FROM ansv_accidents").fetchone()[0]
    assert built_count == 1

    pdf_rep = conn.execute(
        "SELECT status FROM ansv_reports WHERE case_id='I-PDF_2024-01-01'"
    ).fetchone()
    scan_rep = conn.execute(
        "SELECT status FROM ansv_reports WHERE case_id='I-SCAN_2024-01-02'"
    ).fetchone()
    assert pdf_rep["status"] == db.STATUS_BUILT
    assert scan_rep["status"] == db.STATUS_SKIPPED
