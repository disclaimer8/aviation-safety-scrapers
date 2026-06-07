# tests/test_pipeline.py
import pytest

from gpiaaf_ingest import gpiaaf, db, pdf
from gpiaaf_ingest.pipeline import discover, fetch, build

ROOT = gpiaaf.LISTING_ROOT
YEAR_URL = ROOT + "/de-2010-a-2019/2017"
DOC_A = YEAR_URL + "?v=AAA"
DOC_B = ROOT + "/de-2020-a-2026/2022?v=EEE"


def _row(case_id, doc_url, has_report=True, pdf_id=None, reg="CS-AVA",
         event_date="2017-08-02", aircraft="Cessna 152", local="Caparica",
         classification="Acidente"):
    return {
        "case_id": case_id, "event_date": event_date,
        "classification": classification, "aircraft": aircraft,
        "registration": reg, "location": local,
        "doc_url": doc_url if has_report else None,
        "doc_title": "d055927.pdf" if has_report else None,
        "pdf_id": pdf_id, "has_report": has_report, "year": "2017",
    }


def _browser(make_browser, rows=None, **kw):
    rows = rows if rows is not None else [
        _row("04-accid-2017", DOC_A),
        # bulletin-only metadata row
        _row(None, None, has_report=False, reg="D-GZBX",
             aircraft="Gulfstream", local="Cascais", classification="Incidente"),
    ]
    return make_browser(year_urls=[YEAR_URL], year_rows={YEAR_URL: rows}, **kw)


# ─── discover ─────────────────────────────────────────────────────────────────

def test_discover_inserts_rows(conn, make_browser, monkeypatch):
    monkeypatch.setattr(gpiaaf, "DELAY", 0)
    n = discover(conn, _browser(make_browser))
    assert n == 2

    a = conn.execute("SELECT * FROM gpiaaf_reports WHERE case_id='04-accid-2017'").fetchone()
    assert a["status"] == db.STATUS_NEW
    assert a["doc_url"] == DOC_A
    assert a["event_date"] == "2017-08-02"
    assert a["registration"] == "CS-AVA"
    assert a["source_url"] == YEAR_URL
    assert a["classification"] == "Acidente"

    # bulletin-only row → no_report status, fallback case_id
    b = conn.execute(
        "SELECT * FROM gpiaaf_reports WHERE status=?", (db.STATUS_NO_REPORT,)
    ).fetchone()
    assert b is not None
    assert b["doc_url"] is None
    assert b["case_id"].startswith("gpiaaf-")  # fallback (no process number)
    assert b["registration"] == "D-GZBX"


def test_discover_idempotent(conn, make_browser, monkeypatch):
    monkeypatch.setattr(gpiaaf, "DELAY", 0)
    n1 = discover(conn, _browser(make_browser))
    n2 = discover(conn, _browser(make_browser))
    assert n1 == 2
    assert n2 == 0
    total = conn.execute("SELECT COUNT(*) FROM gpiaaf_reports").fetchone()[0]
    assert total == 2


def test_discover_case_id_collision_suffix(conn, make_browser, monkeypatch):
    monkeypatch.setattr(gpiaaf, "DELAY", 0)
    rows = [
        _row("04-accid-2017", DOC_A),
        _row("04-accid-2017", DOC_B),  # same case_id, different doc
    ]
    discover(conn, _browser(make_browser, rows=rows))
    ids = sorted(r["case_id"] for r in conn.execute(
        "SELECT case_id FROM gpiaaf_reports"))
    assert "04-accid-2017" in ids
    assert "04-accid-2017-2" in ids


def test_discover_max_years(conn, make_browser, monkeypatch):
    monkeypatch.setattr(gpiaaf, "DELAY", 0)
    br = make_browser(
        year_urls=[YEAR_URL, ROOT + "/de-2020-a-2026/2022"],
        year_rows={YEAR_URL: [_row("04-accid-2017", DOC_A)],
                   ROOT + "/de-2020-a-2026/2022": [_row("01-accid-2022", DOC_B)]},
    )
    n = discover(conn, br, max_years=1)
    assert n == 1


# ─── fetch (S3-capture flow, mocked browser events) ───────────────────────────

def _insert_new(conn, case_id, doc_url, pdf_id=None, status=db.STATUS_NEW):
    conn.execute(
        "INSERT INTO gpiaaf_reports (case_id, doc_url, pdf_id, status, "
        " discovered_at, updated_at) VALUES (?,?,?,?,?,?)",
        (case_id, doc_url, pdf_id, status, db.now_ms(), db.now_ms()),
    )
    conn.commit()


def test_fetch_captures_pdf_and_parses(conn, make_browser, monkeypatch, tmp_path):
    monkeypatch.setattr(gpiaaf, "DELAY", 0)
    monkeypatch.setattr(pdf, "extract_text", lambda p: "T" * 5000)
    _insert_new(conn, "04-accid-2017", DOC_A)

    # browser returns the presigned S3 url + captured d-number
    br = make_browser(pdfs={DOC_A: ("https://s3/d055927.pdf?X-Amz-Expires=60",
                                    "d055927")})
    n = fetch(conn, br, pdf_dir=str(tmp_path))
    assert n == 1
    assert br.captured == [DOC_A]   # the ?v= route was followed

    row = conn.execute("SELECT * FROM gpiaaf_reports WHERE case_id='04-accid-2017'").fetchone()
    assert row["status"] == db.STATUS_PARSED
    assert row["source_tier"] == "pdf"
    assert len(row["narrative_text"]) == 5000
    assert row["pdf_id"] == "d055927"
    assert row["pdf_url"].startswith("https://s3/")
    assert row["pdf_path"].endswith("d055927.pdf")  # named by stable d-number


def test_fetch_scanned_tier_short_text(conn, make_browser, monkeypatch, tmp_path):
    monkeypatch.setattr(gpiaaf, "DELAY", 0)
    monkeypatch.setattr(pdf, "extract_text", lambda p: "tiny")  # < MIN_NARRATIVE
    _insert_new(conn, "x-accid-2017", DOC_A)
    fetch(conn, make_browser(), pdf_dir=str(tmp_path))
    row = conn.execute("SELECT * FROM gpiaaf_reports WHERE case_id='x-accid-2017'").fetchone()
    assert row["source_tier"] == "scanned"
    assert row["status"] == db.STATUS_PARSED


def test_fetch_capture_failure_marks_parsed_none(conn, make_browser,
                                                 monkeypatch, tmp_path):
    monkeypatch.setattr(gpiaaf, "DELAY", 0)
    _insert_new(conn, "y-accid-2017", DOC_A)
    # capture_pdf raises (e.g. S3 url expired / download never fired)
    br = make_browser(pdfs={DOC_A: RuntimeError("download timeout")})
    fetch(conn, br, pdf_dir=str(tmp_path))
    row = conn.execute("SELECT * FROM gpiaaf_reports WHERE case_id='y-accid-2017'").fetchone()
    assert row["status"] == db.STATUS_PARSED
    assert row["source_tier"] == "none"
    assert row["pdf_path"] is None


def test_fetch_skips_no_report_rows(conn, make_browser, monkeypatch, tmp_path):
    monkeypatch.setattr(gpiaaf, "DELAY", 0)
    # a no_report (bulletin-only) row must NOT be fetched
    _insert_new(conn, "gpiaaf-deadbeef", None, status=db.STATUS_NO_REPORT)
    n = fetch(conn, make_browser(), pdf_dir=str(tmp_path))
    assert n == 0


def test_fetch_max_pdfs(conn, make_browser, monkeypatch, tmp_path):
    monkeypatch.setattr(gpiaaf, "DELAY", 0)
    monkeypatch.setattr(pdf, "extract_text", lambda p: "T" * 1000)
    _insert_new(conn, "a-accid-2017", DOC_A)
    _insert_new(conn, "b-accid-2017", DOC_B)
    n = fetch(conn, make_browser(), pdf_dir=str(tmp_path), max_pdfs=1)
    assert n == 1


# ─── build ────────────────────────────────────────────────────────────────────

def _insert_parsed(conn, case_id, narrative, tier="pdf",
                   event_date="2017-08-02", aircraft="Cessna 152",
                   registration="CS-AVA", location="Caparica",
                   classification="Acidente", source_url=YEAR_URL):
    conn.execute(
        "INSERT INTO gpiaaf_reports "
        "(case_id, source_url, narrative_text, source_tier, event_date, "
        " aircraft, registration, location, classification, status, "
        " discovered_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (case_id, source_url, narrative, tier, event_date, aircraft,
         registration, location, classification, db.STATUS_PARSED,
         db.now_ms(), db.now_ms()),
    )
    conn.commit()


def test_build_creates_accident(conn):
    _insert_parsed(conn, "04-accid-2017", "N" * 500)
    n = build(conn)
    assert n == 1
    acc = conn.execute("SELECT * FROM gpiaaf_accidents WHERE case_id='04-accid-2017'").fetchone()
    assert acc["country"] == "PT"
    assert acc["event_date"] == "2017-08-02"
    assert acc["aircraft"] == "Cessna 152"
    assert acc["registration"] == "CS-AVA"
    assert acc["location"] == "Caparica"
    assert acc["source_url"] == YEAR_URL
    assert acc["report_type"] == "Acidente"
    assert acc["site_slug"].startswith("crash-")
    rep = conn.execute("SELECT status FROM gpiaaf_reports WHERE case_id='04-accid-2017'").fetchone()
    assert rep["status"] == db.STATUS_BUILT


def test_build_short_narrative_skipped(conn):
    _insert_parsed(conn, "05-accid-2017", "too short")
    assert build(conn) == 0
    rep = conn.execute("SELECT status FROM gpiaaf_reports WHERE case_id='05-accid-2017'").fetchone()
    assert rep["status"] == db.STATUS_SKIPPED
    assert conn.execute("SELECT * FROM gpiaaf_accidents").fetchone() is None


def test_build_floor_is_300(conn):
    _insert_parsed(conn, "06-accid-2017", "X" * 299)
    assert build(conn) == 0
    _insert_parsed(conn, "07-accid-2017", "X" * 300)
    assert build(conn) == 1
