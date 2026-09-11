# tests/test_pipeline.py
"""Pipeline tests for aaicnp discover -> fetch -> parse -> build."""
import os

from aaicnp_ingest import aaicnp, db, pipeline
from aaicnp_ingest.pdf import MIN_NARRATIVE
from aaicnp_ingest.pipeline import _SCANNED_FLOOR, _NARRATIVE_FLOOR


def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


# ── discover ────────────────────────────────────────────────────────────────

def test_discover_inserts_collected_urls(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(aaicnp, "DELAY", 0)
    urls = [
        ("https://giwmscdnone.gov.np/media/pdf_upload/REPORT_OF_9N-AMI_x.pdf", "Final Report 9N-AMI"),
        ("https://caanepal.gov.np/storage/app/media/news/Pokhara_x.pdf", "Accident Report Pokhara"),
    ]
    monkeypatch.setattr(pipeline, "_collect_report_urls", lambda conn, client: urls)

    assert pipeline.discover(conn, None) == 2
    rows = conn.execute("SELECT case_id, pdf_url, status, lang FROM aaicnp_reports").fetchall()
    assert len(rows) == 2
    assert all(r["status"] == db.STATUS_NEW for r in rows)
    assert all(r["lang"] == "en" for r in rows)
    assert all(r["pdf_url"] for r in rows)


def test_discover_idempotent_by_pdf_url(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(aaicnp, "DELAY", 0)
    urls = [("https://giwmscdnone.gov.np/media/pdf_upload/A_9N-AMI_x.pdf", "Report")]
    monkeypatch.setattr(pipeline, "_collect_report_urls", lambda conn, client: urls)

    assert pipeline.discover(conn, None) == 1
    assert pipeline.discover(conn, None) == 0  # idempotent
    assert conn.execute("SELECT COUNT(*) FROM aaicnp_reports").fetchone()[0] == 1


def test_discover_full_flag_accepted(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(aaicnp, "DELAY", 0)
    monkeypatch.setattr(pipeline, "_collect_report_urls",
                        lambda conn, client: [("https://giwmscdnone.gov.np/x_a.pdf", "Accident Report")])
    assert pipeline.discover(conn, None, full=True) == 1


# ── fetch ───────────────────────────────────────────────────────────────────

def _seed_new(conn, case_id, pdf_url):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO aaicnp_reports (case_id, pdf_url, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, pdf_url, db.STATUS_NEW, ts, ts),
    )
    conn.commit()


def test_fetch_downloads_and_advances(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "AAIC-NP-1-2026", "https://giwmscdnone.gov.np/x.pdf")

    calls = []
    def _dl(client, url, dest):
        calls.append((url, dest))
        open(dest, "wb").write(b"%PDF")
    monkeypatch.setattr(aaicnp, "download", _dl)
    monkeypatch.setattr(aaicnp, "DELAY", 0)

    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    row = conn.execute("SELECT status, pdf_path FROM aaicnp_reports WHERE case_id='AAIC-NP-1-2026'").fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert os.path.exists(row["pdf_path"])
    assert len(calls) == 1


def test_fetch_download_failure_keeps_row_new(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "AAIC-NP-1-2026", "https://giwmscdnone.gov.np/x.pdf")
    monkeypatch.setattr(aaicnp, "download",
                        lambda c, u, d: (_ for _ in ()).throw(RuntimeError("timeout")))
    monkeypatch.setattr(aaicnp, "DELAY", 0)
    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    assert conn.execute("SELECT status FROM aaicnp_reports WHERE case_id='AAIC-NP-1-2026'").fetchone()["status"] == db.STATUS_NEW


def test_fetch_isolates_per_row_errors(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "AAIC-NP-1-2026", "https://giwmscdnone.gov.np/a.pdf")
    _seed_new(conn, "9N-XXX-2020-01-01", "https://giwmscdnone.gov.np/b.pdf")

    def _dl(client, url, dest):
        if url.endswith("a.pdf"):
            raise RuntimeError("403")
        open(dest, "wb").write(b"%PDF")
    monkeypatch.setattr(aaicnp, "download", _dl)
    monkeypatch.setattr(aaicnp, "DELAY", 0)

    assert pipeline.fetch(conn, None, str(tmp_path)) == 2
    assert conn.execute("SELECT status FROM aaicnp_reports WHERE case_id='AAIC-NP-1-2026'").fetchone()["status"] == db.STATUS_NEW
    assert conn.execute("SELECT status FROM aaicnp_reports WHERE case_id='9N-XXX-2020-01-01'").fetchone()["status"] == db.STATUS_FETCHED


# ── parse ───────────────────────────────────────────────────────────────────

def _seed_fetched(conn, case_id, pdf_path, pdf_url="https://giwmscdnone.gov.np/x.pdf"):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO aaicnp_reports (case_id, status, pdf_path, pdf_url, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (case_id, db.STATUS_FETCHED, pdf_path, pdf_url, ts, ts),
    )
    conn.commit()


def test_parse_long_narrative_and_rekey(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "PROVISIONAL-SLUG", "x.pdf")
    text = "Aircraft Accident Investigation Report 1/2026\n" + ("X" * MIN_NARRATIVE)
    monkeypatch.setattr(pipeline, "extract_text", lambda p: text)

    assert pipeline.parse(conn) == 1
    # provisional id replaced by intrinsic ref-based id
    assert conn.execute("SELECT 1 FROM aaicnp_reports WHERE case_id='PROVISIONAL-SLUG'").fetchone() is None
    row = conn.execute("SELECT source_tier, status, registration FROM aaicnp_reports WHERE case_id='AAIC-NP-1-2026'").fetchone()
    assert row is not None
    assert row["source_tier"] == "pdf"
    assert row["status"] == db.STATUS_PARSED


def test_parse_scanned_tier(monkeypatch):
    """Text below the scanned floor (~500) -> tier 'scanned'."""
    conn = _conn()
    _seed_fetched(conn, "9N-ABC-2010-01-01", "x.pdf")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "9N-ABC " + ("y" * 100))
    assert pipeline.parse(conn) == 1
    row = conn.execute("SELECT source_tier FROM aaicnp_reports WHERE registration='9N-ABC'").fetchone()
    assert row["source_tier"] == "scanned"


def test_parse_short_tier(monkeypatch):
    """Text >= scanned floor but < MIN_NARRATIVE -> 'short'."""
    conn = _conn()
    _seed_fetched(conn, "9N-DEF-2011-01-01", "x.pdf")
    body = "9N-DEF accident " + ("z" * (_SCANNED_FLOOR + 10))
    monkeypatch.setattr(pipeline, "extract_text", lambda p: body)
    assert pipeline.parse(conn) == 1
    row = conn.execute("SELECT source_tier FROM aaicnp_reports WHERE registration='9N-DEF'").fetchone()
    assert row["source_tier"] == "short"


def test_parse_no_pdf_none_tier(monkeypatch):
    conn = _conn()
    # explicit pdf_url whose filename slug is the fallback intrinsic id
    _seed_fetched(conn, "PROV", None, pdf_url="https://giwmscdnone.gov.np/9n-ghi-2012-report.pdf")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "")
    assert pipeline.parse(conn) == 1
    row = conn.execute("SELECT source_tier, status FROM aaicnp_reports").fetchone()
    assert row["source_tier"] == "none"
    assert row["status"] == db.STATUS_PARSED


def test_parse_dedup_merges_duplicate_intrinsic_id(monkeypatch):
    """Two staging rows resolving to the same intrinsic id -> one survives."""
    conn = _conn()
    _seed_fetched(conn, "SLUG-A", "a.pdf", pdf_url="https://giwmscdnone.gov.np/a.pdf")
    _seed_fetched(conn, "SLUG-B", "b.pdf", pdf_url="https://giwmscdnone.gov.np/b.pdf")
    text = "Aircraft Accident Investigation Report 9/2030\n" + ("X" * MIN_NARRATIVE)
    monkeypatch.setattr(pipeline, "extract_text", lambda p: text)

    pipeline.parse(conn)
    rows = conn.execute("SELECT case_id FROM aaicnp_reports").fetchall()
    assert len(rows) == 1
    assert rows[0]["case_id"] == "AAIC-NP-9-2030"


# ── build ───────────────────────────────────────────────────────────────────

def _seed_parsed(conn, case_id, *, narrative="", tier="pdf", aircraft=None,
                 registration=None, location=None, date=None, event_class=None,
                 pdf_url=None, report_url=None):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO aaicnp_reports "
        "(case_id, aircraft, registration, location, date_of_occurrence, "
        "narrative_text, source_tier, event_class, pdf_url, report_url, status, "
        "discovered_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (case_id, aircraft, registration, location, date, narrative, tier,
         event_class, pdf_url, report_url, db.STATUS_PARSED, ts, ts),
    )
    conn.commit()


def test_build_creates_accident_row_country_np():
    conn = _conn()
    _seed_parsed(conn, "AAIC-NP-1-2026", narrative="N" * 300, tier="pdf",
                 aircraft="AS350 B3e", registration="9N-AMS", location="Lobuche",
                 date="2025-10-29", event_class="Accident",
                 pdf_url="https://giwmscdnone.gov.np/x.pdf")
    assert pipeline.build(conn) == 1
    acc = conn.execute("SELECT * FROM aaicnp_accidents WHERE case_id='AAIC-NP-1-2026'").fetchone()
    assert acc["country"] == "NP"
    assert acc["event_date"] == "2025-10-29"
    assert acc["registration"] == "9N-AMS"
    assert acc["report_type"] == "Accident"
    assert acc["source_url"] == "https://giwmscdnone.gov.np/x.pdf"
    assert acc["site_slug"].startswith("crash-")
    assert conn.execute("SELECT status FROM aaicnp_reports WHERE case_id='AAIC-NP-1-2026'").fetchone()["status"] == db.STATUS_BUILT


def test_build_skips_scanned_tier():
    conn = _conn()
    _seed_parsed(conn, "9N-OLD-1990-01-01", narrative="X" * 300, tier="scanned",
                 event_class="Accident")
    assert pipeline.build(conn) == 0
    assert conn.execute("SELECT status FROM aaicnp_reports WHERE case_id='9N-OLD-1990-01-01'").fetchone()["status"] == db.STATUS_SKIPPED
    assert conn.execute("SELECT COUNT(*) FROM aaicnp_accidents").fetchone()[0] == 0


def test_build_skips_below_floor():
    conn = _conn()
    _seed_parsed(conn, "9N-Z-2000-01-01", narrative="X" * (_NARRATIVE_FLOOR - 1),
                 tier="pdf", event_class="Accident")
    assert pipeline.build(conn) == 0
    assert conn.execute("SELECT status FROM aaicnp_reports WHERE case_id='9N-Z-2000-01-01'").fetchone()["status"] == db.STATUS_SKIPPED


def test_build_source_url_falls_back_to_report_url():
    conn = _conn()
    _seed_parsed(conn, "9N-FB-2001-01-01", narrative="N" * 300, tier="pdf",
                 event_class="Accident", pdf_url=None,
                 report_url="https://caanepal.gov.np/news-detail/post/x")
    pipeline.build(conn)
    acc = conn.execute("SELECT source_url FROM aaicnp_accidents WHERE case_id='9N-FB-2001-01-01'").fetchone()
    assert acc["source_url"] == "https://caanepal.gov.np/news-detail/post/x"


def test_build_mixed_rows():
    conn = _conn()
    _seed_parsed(conn, "A", narrative="Z" * (pipeline._NARRATIVE_FLOOR + 100), tier="pdf", event_class="Accident")
    _seed_parsed(conn, "B", narrative="Z" * (pipeline._NARRATIVE_FLOOR + 100), tier="short", event_class="Serious incident")
    _seed_parsed(conn, "C", narrative="", tier="none", event_class="Accident")
    assert pipeline.build(conn) == 2
    assert conn.execute("SELECT COUNT(*) FROM aaicnp_accidents").fetchone()[0] == 2
    assert conn.execute("SELECT status FROM aaicnp_reports WHERE case_id='C'").fetchone()["status"] == db.STATUS_SKIPPED
