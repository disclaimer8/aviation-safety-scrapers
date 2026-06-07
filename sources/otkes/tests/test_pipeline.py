# tests/test_pipeline.py
import pytest

from otkes_ingest import otkes, db, pdf
from otkes_ingest.pipeline import discover, fetch, build


YEAR_URL = ("https://turvallisuustutkinta.fi/fi/index/tutkintaselostukset/"
            "ilmailuonnettomuuksientutkinta/tutkintaselostuksetvuosittain/2024.html")
DET_A = ("https://turvallisuustutkinta.fi/fi/index/tutkintaselostukset/"
         "ilmailuonnettomuuksientutkinta/tutkintaselostuksetvuosittain/2024/a.html")
DET_B = ("https://turvallisuustutkinta.fi/fi/index/tutkintaselostukset/"
         "ilmailuonnettomuuksientutkinta/tutkintaselostuksetvuosittain/2024/b.html")

PDF_URL = "https://turvallisuustutkinta.fi/material/sites/otkes/otkes/h/L2024-01_Tutkintaselostus.pdf"


def _browser(make_browser, **detail_overrides):
    details = {
        DET_A: {
            "case_number": "L2024-01", "occurrence_type": "Lentokoneet",
            "event_date": "2024-01-15", "publish_date": "2024-09-30",
            "summary": "Pitkä suomenkielinen tiivistelmä. " * 20,
            "title": "Onnettomuus OH-LZA Helsinki", "pdf_url": PDF_URL,
            "registration": "OH-LZA",
        },
        DET_B: {  # 'selvitys' with no case number, no PDF, on-page summary only
            "case_number": None, "occurrence_type": "Helikopterit",
            "event_date": "2024-07-19", "publish_date": "2024-10-15",
            "summary": "Lyhyt selvitys ilman PDF:ää. " * 20,
            "title": "Selvitys Muhos", "pdf_url": None,
            "registration": None,
        },
    }
    details.update(detail_overrides)
    return make_browser(
        listings=[YEAR_URL],
        year_pages={YEAR_URL: [DET_A, DET_B]},
        details=details,
    )


# ─── discover ─────────────────────────────────────────────────────────────────

def test_discover_inserts_rows(conn, make_browser, monkeypatch):
    monkeypatch.setattr(otkes, "DELAY", 0)
    n = discover(conn, _browser(make_browser))
    assert n == 2

    a = conn.execute("SELECT * FROM otkes_reports WHERE detail_url=?",
                     (DET_A,)).fetchone()
    assert a["case_id"] == "l2024-01"
    assert a["event_date"] == "2024-01-15"
    assert a["pdf_url"] == PDF_URL
    assert a["status"] == db.STATUS_NEW
    assert a["registration"] == "OH-LZA"

    b = conn.execute("SELECT * FROM otkes_reports WHERE detail_url=?",
                     (DET_B,)).fetchone()
    # no Tutkintanumero → fallback case_id
    assert b["case_id"].startswith("otkes-")
    assert b["pdf_url"] is None
    assert b["page_summary"].startswith("Lyhyt selvitys")


def test_discover_idempotent(conn, make_browser, monkeypatch):
    monkeypatch.setattr(otkes, "DELAY", 0)
    n1 = discover(conn, _browser(make_browser))
    n2 = discover(conn, _browser(make_browser))
    assert n1 == 2
    assert n2 == 0
    total = conn.execute("SELECT COUNT(*) FROM otkes_reports").fetchone()[0]
    assert total == 2


def test_discover_case_id_collision_suffix(conn, make_browser, monkeypatch):
    """Two details normalising to the same case_id get a -2 suffix."""
    monkeypatch.setattr(otkes, "DELAY", 0)
    br = _browser(
        make_browser,
        **{DET_B: {"case_number": "L2024-01", "occurrence_type": "X",
                   "event_date": "2024-02-02", "publish_date": None,
                   "summary": "s" * 400, "title": "Dup", "pdf_url": None,
                   "registration": None}},
    )
    discover(conn, br)
    ids = sorted(r["case_id"] for r in conn.execute(
        "SELECT case_id FROM otkes_reports"))
    assert "l2024-01" in ids
    assert "l2024-01-2" in ids


def test_discover_max_details(conn, make_browser, monkeypatch):
    monkeypatch.setattr(otkes, "DELAY", 0)
    n = discover(conn, _browser(make_browser), max_details=1)
    assert n == 1


# ─── fetch ────────────────────────────────────────────────────────────────────

def _insert_new(conn, case_id, detail_url, pdf_url=None, page_summary=""):
    conn.execute(
        "INSERT INTO otkes_reports "
        "(case_id, detail_url, pdf_url, page_summary, status, "
        " discovered_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (case_id, detail_url, pdf_url, page_summary, db.STATUS_NEW,
         db.now_ms(), db.now_ms()),
    )
    conn.commit()


def test_fetch_pdf_tier(conn, make_client, monkeypatch, tmp_path):
    monkeypatch.setattr(otkes, "DELAY", 0)
    monkeypatch.setattr(pdf, "extract_text", lambda p: "T" * 5000)
    _insert_new(conn, "l2024-01", DET_A, pdf_url=PDF_URL)

    n = fetch(conn, make_client(), pdf_dir=str(tmp_path))
    assert n == 1
    row = conn.execute("SELECT * FROM otkes_reports WHERE detail_url=?",
                       (DET_A,)).fetchone()
    assert row["status"] == db.STATUS_PARSED
    assert row["source_tier"] == "pdf"
    assert len(row["narrative_text"]) == 5000
    assert row["pdf_path"].endswith(".pdf")


def test_fetch_summary_tier_no_pdf(conn, make_client, monkeypatch, tmp_path):
    monkeypatch.setattr(otkes, "DELAY", 0)
    _insert_new(conn, "otkes-abcd1234", DET_B, pdf_url=None,
                page_summary="Selvityksen tiivistelmä. " * 30)

    fetch(conn, make_client(), pdf_dir=str(tmp_path))
    row = conn.execute("SELECT * FROM otkes_reports WHERE detail_url=?",
                       (DET_B,)).fetchone()
    assert row["status"] == db.STATUS_PARSED
    assert row["source_tier"] == "summary"
    assert row["narrative_text"].startswith("Selvityksen")


def test_fetch_scanned_pdf_falls_back_to_summary(conn, make_client,
                                                 monkeypatch, tmp_path):
    monkeypatch.setattr(otkes, "DELAY", 0)
    monkeypatch.setattr(pdf, "extract_text", lambda p: "tiny")  # < MIN_NARRATIVE
    _insert_new(conn, "l2024-09", DET_A, pdf_url=PDF_URL,
                page_summary="Sivun tiivistelmä korvaa skannatun. " * 20)

    fetch(conn, make_client(), pdf_dir=str(tmp_path))
    row = conn.execute("SELECT * FROM otkes_reports WHERE detail_url=?",
                       (DET_A,)).fetchone()
    assert row["source_tier"] == "scanned"
    # narrative should be the page summary (pdf text was too short)
    assert "tiivistelmä" in row["narrative_text"]


def test_fetch_pdf_download_failure_uses_summary(conn, make_client,
                                                 monkeypatch, tmp_path):
    monkeypatch.setattr(otkes, "DELAY", 0)

    def boom(client, url, dest):
        raise RuntimeError("404")
    monkeypatch.setattr(otkes, "download_pdf", boom)
    _insert_new(conn, "l2024-10", DET_A, pdf_url=PDF_URL,
                page_summary="Varatiivistelmä. " * 30)

    fetch(conn, make_client(), pdf_dir=str(tmp_path))
    row = conn.execute("SELECT * FROM otkes_reports WHERE detail_url=?",
                       (DET_A,)).fetchone()
    assert row["status"] == db.STATUS_PARSED
    assert row["source_tier"] == "summary"
    assert row["pdf_path"] is None


# ─── build ────────────────────────────────────────────────────────────────────

def _insert_parsed(conn, case_id, detail_url, narrative,
                   tier="pdf", event_date="2024-01-15",
                   title="Onnettomuus OH-LZA", registration="OH-LZA",
                   occurrence_type="Lentokoneet"):
    conn.execute(
        "INSERT INTO otkes_reports "
        "(case_id, detail_url, narrative_text, source_tier, event_date, "
        " title, registration, occurrence_type, status, discovered_at, "
        " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (case_id, detail_url, narrative, tier, event_date, title,
         registration, occurrence_type, db.STATUS_PARSED,
         db.now_ms(), db.now_ms()),
    )
    conn.commit()


def test_build_creates_accident(conn):
    _insert_parsed(conn, "l2024-01", DET_A, "N" * 500)
    n = build(conn)
    assert n == 1
    acc = conn.execute("SELECT * FROM otkes_accidents WHERE case_id='l2024-01'").fetchone()
    assert acc["country"] == "FI"
    assert acc["event_date"] == "2024-01-15"
    assert acc["source_url"] == DET_A
    assert acc["report_type"] == "Lentokoneet"
    assert acc["registration"] == "OH-LZA"
    assert acc["site_slug"].startswith("crash-")
    rep = conn.execute("SELECT status FROM otkes_reports WHERE case_id='l2024-01'").fetchone()
    assert rep["status"] == db.STATUS_BUILT


def test_build_summary_tier_qualifies(conn):
    """A 'summary'-tier row with a long enough narrative still builds."""
    _insert_parsed(conn, "otkes-abcd1234", DET_B, "S" * 500, tier="summary")
    n = build(conn)
    assert n == 1


def test_build_short_narrative_skipped(conn):
    _insert_parsed(conn, "l2024-02", DET_A, "too short")
    n = build(conn)
    assert n == 0
    rep = conn.execute("SELECT status FROM otkes_reports WHERE case_id='l2024-02'").fetchone()
    assert rep["status"] == db.STATUS_SKIPPED
    acc = conn.execute("SELECT * FROM otkes_accidents WHERE case_id='l2024-02'").fetchone()
    assert acc is None


def test_build_floor_is_300(conn):
    _insert_parsed(conn, "l2024-03", DET_A, "X" * 299)
    assert build(conn) == 0
    _insert_parsed(conn, "l2024-04", DET_B, "X" * 300)
    assert build(conn) == 1
