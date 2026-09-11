"""Pipeline tests for aaibmn discover -> fetch -> parse -> build."""
import os

from aaibmn_ingest import aaibmn, db, pipeline
from aaibmn_ingest.pdf import MIN_NARRATIVE


def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


_FAKE_ROWS = [
    {
        "case_id": "ju-1088-2024-02-27",
        "pdf_url": "https://aaib.gov.mn/uploads/old/a.pdf",
        "pdf_url_en": "https://aaib.gov.mn/uploads/old/a.pdf",
        "pdf_url_es": None, "report_url": None,
        "title": "(2024.02.27)B737-800,JU-1088 INCIDENT WARNING",
        "event_class": "Incident", "aircraft": None,
        "registration": "JU-1088", "date_of_occurrence": "2024-02-27",
        "location": None, "lang": "en",
    },
    {
        "case_id": "ei-cxv-2020-07-02",
        "pdf_url": "https://aaib.gov.mn/uploads/old/b.pdf",
        "pdf_url_en": "https://aaib.gov.mn/uploads/old/b.pdf",
        "pdf_url_es": None, "report_url": None,
        "title": "(2020.07.02) B737-800, EI-CXV serious incident shutdown",
        "event_class": "Serious incident", "aircraft": None,
        "registration": "EI-CXV", "date_of_occurrence": "2020-07-02",
        "location": None, "lang": "en",
    },
]


def _patch_pages(monkeypatch, rows):
    monkeypatch.setattr(aaibmn, "iter_listing_pages",
                        lambda client: [("u1", "<html>")])
    monkeypatch.setattr(aaibmn, "parse_listing", lambda html: rows)


# ── discover ────────────────────────────────────────────────────────────────
def test_discover_inserts(monkeypatch):
    conn = _conn()
    _patch_pages(monkeypatch, _FAKE_ROWS)
    assert pipeline.discover(conn, object()) == 2
    rows = conn.execute("SELECT case_id, registration, date_of_occurrence, "
                        "lang, status FROM aaibmn_reports ORDER BY case_id").fetchall()
    assert len(rows) == 2
    assert all(r["status"] == db.STATUS_NEW for r in rows)
    r = next(x for x in rows if x["case_id"] == "ju-1088-2024-02-27")
    assert r["registration"] == "JU-1088"
    assert r["date_of_occurrence"] == "2024-02-27"
    assert r["lang"] == "en"


def test_discover_idempotent(monkeypatch):
    conn = _conn()
    _patch_pages(monkeypatch, _FAKE_ROWS)
    assert pipeline.discover(conn, object()) == 2
    assert pipeline.discover(conn, object()) == 0
    assert conn.execute("SELECT COUNT(*) FROM aaibmn_reports").fetchone()[0] == 2


# ── fetch ───────────────────────────────────────────────────────────────────
def _seed_new(conn, case_id, pdf_url):
    ts = db.now_ms()
    conn.execute("INSERT INTO aaibmn_reports (case_id, pdf_url, status, "
                 "discovered_at, updated_at) VALUES (?,?,?,?,?)",
                 (case_id, pdf_url, db.STATUS_NEW, ts, ts))
    conn.commit()


def test_fetch_downloads_and_advances(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "ju-1088-2024-02-27", "https://aaib.gov.mn/x.pdf")
    monkeypatch.setattr(aaibmn, "download",
                        lambda c, u, d: open(d, "wb").write(b"%PDF"))
    monkeypatch.setattr(aaibmn, "DELAY", 0)
    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    row = conn.execute("SELECT status, pdf_path FROM aaibmn_reports").fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert os.path.exists(row["pdf_path"])


def test_fetch_failure_keeps_new(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "x", "https://aaib.gov.mn/x.pdf")
    monkeypatch.setattr(aaibmn, "download",
                        lambda c, u, d: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(aaibmn, "DELAY", 0)
    pipeline.fetch(conn, None, str(tmp_path))
    assert conn.execute("SELECT status FROM aaibmn_reports").fetchone()["status"] == db.STATUS_NEW


# ── parse ───────────────────────────────────────────────────────────────────
def _seed_fetched(conn, case_id, pdf_path):
    ts = db.now_ms()
    conn.execute("INSERT INTO aaibmn_reports (case_id, status, pdf_path, "
                 "discovered_at, updated_at) VALUES (?,?,?,?,?)",
                 (case_id, db.STATUS_FETCHED, pdf_path, ts, ts))
    conn.commit()


def test_parse_scanned_pdf_tier(monkeypatch):
    """PDF present but no text → tier='scanned' (the Mongolia case)."""
    conn = _conn()
    _seed_fetched(conn, "x", "x.pdf")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "")
    pipeline.parse(conn)
    row = conn.execute("SELECT source_tier, status FROM aaibmn_reports").fetchone()
    assert row["source_tier"] == "scanned"
    assert row["status"] == db.STATUS_PARSED


def test_parse_long_pdf_tier(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "x", "x.pdf")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "Y" * MIN_NARRATIVE)
    pipeline.parse(conn)
    assert conn.execute("SELECT source_tier FROM aaibmn_reports").fetchone()["source_tier"] == "pdf"


def test_parse_no_pdf_tier_none(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "x", None)
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "Y" * 999)
    pipeline.parse(conn)
    assert conn.execute("SELECT source_tier FROM aaibmn_reports").fetchone()["source_tier"] == "none"


# ── build ───────────────────────────────────────────────────────────────────
def _seed_parsed(conn, case_id, *, title="", narrative="", registration=None,
                 date=None, event_class=None, source_tier="scanned", pdf_url=None):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO aaibmn_reports (case_id, title, registration, "
        "date_of_occurrence, narrative_text, event_class, source_tier, pdf_url, "
        "status, discovered_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (case_id, title, registration, date, narrative, event_class,
         source_tier, pdf_url, db.STATUS_PARSED, ts, ts))
    conn.commit()


def test_build_scanned_falls_back_to_title():
    """Scanned PDF (empty narrative) → title used as narrative; still built."""
    conn = _conn()
    _seed_parsed(conn, "ju-1088-2024-02-27",
                 title="(2024.02.27)B737-800,JU-1088 INCIDENT IN FLIGHT CABIN ALTITUDE WARNING",
                 narrative="", registration="JU-1088", date="2024-02-27",
                 event_class="Incident", source_tier="scanned",
                 pdf_url="https://aaib.gov.mn/uploads/old/a.pdf")
    assert pipeline.build(conn) == 1
    acc = conn.execute("SELECT * FROM aaibmn_accidents").fetchone()
    assert acc["country"] == "MN"
    assert acc["event_date"] == "2024-02-27"
    assert acc["registration"] == "JU-1088"
    assert acc["report_type"] == "Incident"
    assert "JU-1088" in acc["narrative_text"]  # came from title
    assert acc["source_url"] == "https://aaib.gov.mn/uploads/old/a.pdf"
    assert acc["site_slug"].startswith("crash-")


def test_build_uses_real_narrative_when_present():
    conn = _conn()
    long = "N" * 800
    _seed_parsed(conn, "x", title="short", narrative=long,
                 source_tier="pdf", event_class="Accident")
    pipeline.build(conn)
    acc = conn.execute("SELECT narrative_text FROM aaibmn_accidents").fetchone()
    assert acc["narrative_text"] == long


def test_build_skips_when_no_title_no_narrative():
    conn = _conn()
    _seed_parsed(conn, "x", title="", narrative="", source_tier="none")
    assert pipeline.build(conn) == 0
    assert conn.execute("SELECT status FROM aaibmn_reports").fetchone()["status"] == db.STATUS_SKIPPED
    assert conn.execute("SELECT COUNT(*) FROM aaibmn_accidents").fetchone()[0] == 0


def test_build_country_is_mn():
    conn = _conn()
    _seed_parsed(conn, "x", title="A" * 50, event_class="Accident")
    pipeline.build(conn)
    assert conn.execute("SELECT country FROM aaibmn_accidents").fetchone()["country"] == "MN"
