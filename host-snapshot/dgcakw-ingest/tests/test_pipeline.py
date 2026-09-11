# tests/test_pipeline.py
"""Pipeline tests for dgcakw discover -> fetch -> parse -> build."""
import os
import sqlite3

from dgcakw_ingest import db, dgcakw
from dgcakw_ingest.pipeline import discover, fetch, parse, build
from dgcakw_ingest.pdf import MIN_NARRATIVE


def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


# ── discover ───────────────────────────────────────────────────────────────

def test_discover_inserts_known_reports():
    conn = _conn()
    n = discover(conn)
    assert n == len(dgcakw.KNOWN_REPORTS)
    rows = conn.execute("SELECT case_id, status, lang FROM dgcakw_reports ORDER BY case_id").fetchall()
    assert len(rows) == len(dgcakw.KNOWN_REPORTS)
    for r in rows:
        assert r["status"] == db.STATUS_NEW
        assert r["lang"] == "en"


def test_discover_is_idempotent():
    conn = _conn()
    n1 = discover(conn)
    n2 = discover(conn)
    assert n1 == len(dgcakw.KNOWN_REPORTS)
    assert n2 == 0  # second call inserts nothing


def test_discover_sets_archive_url():
    conn = _conn()
    discover(conn)
    rows = conn.execute("SELECT case_id, archive_url FROM dgcakw_reports").fetchall()
    for r in rows:
        assert r["archive_url"], f"{r['case_id']} missing archive_url"
        assert "web.archive.org" in r["archive_url"]


def test_discover_sets_event_dates():
    conn = _conn()
    discover(conn)
    rows = conn.execute("SELECT case_id, date_of_occurrence FROM dgcakw_reports ORDER BY case_id").fetchall()
    dates = {r["case_id"]: r["date_of_occurrence"] for r in rows}
    assert dates.get("DGCAKW-2017-9K-CAK") == "2017-08-27"
    assert dates.get("DGCAKW-2018-9K-AOE") == "2018-02-04"


# ── fetch ──────────────────────────────────────────────────────────────────

class _FakePdfResp:
    status_code = 200
    content = b"%PDF-fake"

    def raise_for_status(self):
        pass


class _FakeClient:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return _FakePdfResp()


def test_fetch_advances_status(tmp_path):
    conn = _conn()
    discover(conn)
    client = _FakeClient()
    n = fetch(conn, client, str(tmp_path))
    assert n == len(dgcakw.KNOWN_REPORTS)
    rows = conn.execute("SELECT status FROM dgcakw_reports").fetchall()
    assert all(r["status"] == db.STATUS_FETCHED for r in rows)
    # PDF files created
    pdfs = list(tmp_path.glob("*.pdf"))
    assert len(pdfs) == len(dgcakw.KNOWN_REPORTS)


def test_fetch_uses_wayback_url(tmp_path):
    conn = _conn()
    discover(conn)
    client = _FakeClient()
    fetch(conn, client, str(tmp_path))
    for call_url in client.calls:
        assert "web.archive.org" in call_url


# ── parse ──────────────────────────────────────────────────────────────────

def test_parse_no_pdf_gives_none_tier(tmp_path):
    conn = _conn()
    discover(conn)
    # Insert a fake fetched row with no pdf_path
    conn.execute(
        "UPDATE dgcakw_reports SET status=?, pdf_path=NULL",
        (db.STATUS_FETCHED,),
    )
    conn.commit()
    parse(conn)
    rows = conn.execute("SELECT source_tier, status FROM dgcakw_reports").fetchall()
    for r in rows:
        assert r["source_tier"] == "none"
        assert r["status"] == db.STATUS_PARSED


def test_parse_real_pdf_is_pdf_tier(tmp_path):
    """Test with the actual downloaded J9-787 PDF if it exists on minipc."""
    pdf_path = "/home/a1/dgcakw-ingest/pdfs/DGCAKW-2017-9K-CAK.pdf"
    if not os.path.exists(pdf_path):
        import pytest
        pytest.skip("Real PDF not present on this machine")

    conn = _conn()
    discover(conn)
    # Manually mark as fetched with real pdf path
    conn.execute(
        "UPDATE dgcakw_reports SET status=?, pdf_path=? WHERE case_id=?",
        (db.STATUS_FETCHED, pdf_path, "DGCAKW-2017-9K-CAK"),
    )
    conn.commit()
    parse(conn)
    row = conn.execute(
        "SELECT source_tier, narrative_text FROM dgcakw_reports WHERE case_id=?",
        ("DGCAKW-2017-9K-CAK",)
    ).fetchone()
    assert row["source_tier"] == "pdf"
    assert len(row["narrative_text"]) >= MIN_NARRATIVE


# ── build ──────────────────────────────────────────────────────────────────

def test_build_emits_accident_row():
    conn = _conn()
    discover(conn)
    narrative = "X" * 400  # above 300-char floor
    conn.execute(
        "UPDATE dgcakw_reports SET status=?, narrative_text=?, source_tier=?",
        (db.STATUS_PARSED, narrative, "pdf"),
    )
    conn.commit()
    n = build(conn)
    assert n == len(dgcakw.KNOWN_REPORTS)
    rows = conn.execute("SELECT case_id, country, narrative_text FROM dgcakw_accidents").fetchall()
    assert len(rows) == len(dgcakw.KNOWN_REPORTS)
    for r in rows:
        assert r["country"] == "KW"
        assert len(r["narrative_text"]) >= 300


def test_build_skips_short_narrative():
    conn = _conn()
    discover(conn)
    conn.execute(
        "UPDATE dgcakw_reports SET status=?, narrative_text=?, source_tier=?",
        (db.STATUS_PARSED, "too short", "short"),
    )
    conn.commit()
    n = build(conn)
    assert n == 0
    # All rows should be STATUS_SKIPPED
    rows = conn.execute("SELECT status FROM dgcakw_reports").fetchall()
    assert all(r["status"] == db.STATUS_SKIPPED for r in rows)


def test_build_country_always_kw():
    conn = _conn()
    discover(conn)
    narrative = "Investigation of serious incident at Kuwait International Airport. " * 10
    conn.execute(
        "UPDATE dgcakw_reports SET status=?, narrative_text=?, source_tier=?",
        (db.STATUS_PARSED, narrative, "pdf"),
    )
    conn.commit()
    build(conn)
    rows = conn.execute("SELECT country FROM dgcakw_accidents").fetchall()
    assert all(r["country"] == "KW" for r in rows)


def test_discover_registrations():
    conn = _conn()
    discover(conn)
    rows = conn.execute("SELECT case_id, registration FROM dgcakw_reports ORDER BY case_id").fetchall()
    regs = {r["case_id"]: r["registration"] for r in rows}
    assert regs.get("DGCAKW-2017-9K-CAK") == "9K-CAK"
    assert regs.get("DGCAKW-2018-9K-AOE") == "9K-AOE"
