# tests/test_pipeline.py
"""Pipeline tests for eccaa discover -> fetch -> parse -> build."""
import os

from eccaa_ingest import eccaa, db, pipeline
from eccaa_ingest.pdf import MIN_NARRATIVE


def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


# parse_listing mock rows: two OECS, one US foreign, one null-meta.
_FAKE_ROWS = [
    {
        "case_id": "J8-SXY-2010-08-05",
        "report_url": eccaa.INDEX_URL,
        "pdf_url": "https://www.eccaa.aero/far/c402.pdf",
        "pdf_url_es": None,
        "pdf_url_en": "https://www.eccaa.aero/far/c402.pdf",
        "aircraft": "Cessna 402-C",
        "registration": "J8-SXY",
        "date_of_occurrence": "2010-08-05",
        "location": None,
        "country": "VC",
        "event_class": "Accident",
        "title": "Final Accident Report Cessna 402-C (J8-SXY) 5 Aug 2010",
    },
    {
        "case_id": "N8862F-2017-01-12",
        "report_url": eccaa.INDEX_URL,
        "pdf_url": "https://www.eccaa.aero/far/pa28.pdf",
        "pdf_url_es": None,
        "pdf_url_en": "https://www.eccaa.aero/far/pa28.pdf",
        "aircraft": "PA-28-151",
        "registration": "N8862F",
        "date_of_occurrence": "2017-01-12",
        "location": None,
        "country": "US",
        "event_class": "Accident",
        "title": "Final Accident Report PA-28-151 (N8862F) 12 Jan 2017",
    },
]


class _FakeResp:
    def __init__(self, body=""):
        self.content = body.encode("utf-8") if isinstance(body, str) else body
        self.status_code = 200

    def raise_for_status(self):
        pass


class _FakeClient:
    def __init__(self, html="<html>"):
        self._html = html
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return _FakeResp(self._html)


# ── discover ────────────────────────────────────────────────────────────────

def test_discover_inserts_rows_with_country(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(eccaa, "parse_listing", lambda html: _FAKE_ROWS)
    client = _FakeClient()

    assert pipeline.discover(conn, client) == 2

    rows = conn.execute(
        "SELECT case_id, country, lang, status, aircraft, registration, "
        "date_of_occurrence, pdf_url FROM eccaa_reports ORDER BY case_id"
    ).fetchall()
    assert len(rows) == 2
    by_id = {r["case_id"]: r for r in rows}
    assert by_id["J8-SXY-2010-08-05"]["country"] == "VC"
    assert by_id["N8862F-2017-01-12"]["country"] == "US"
    assert all(r["status"] == db.STATUS_NEW for r in rows)
    assert all(r["lang"] == "en" for r in rows)


def test_discover_idempotent(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(eccaa, "parse_listing", lambda html: _FAKE_ROWS)
    client = _FakeClient()
    assert pipeline.discover(conn, client) == 2
    assert pipeline.discover(conn, client) == 0
    assert conn.execute("SELECT COUNT(*) FROM eccaa_reports").fetchone()[0] == 2


# ── fetch ─────────────────────────────────────────────────────────────────

def _seed_new(conn, case_id, pdf_url):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO eccaa_reports (case_id, pdf_url, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, pdf_url, db.STATUS_NEW, ts, ts),
    )
    conn.commit()


def test_fetch_downloads_and_advances(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "J8-SXY-2010-08-05", "https://www.eccaa.aero/far/c402.pdf")

    def _fake_download(client, url, dest):
        open(dest, "wb").write(b"%PDF")
    monkeypatch.setattr(eccaa, "download", _fake_download)
    monkeypatch.setattr(eccaa, "DELAY", 0)

    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    row = conn.execute(
        "SELECT status, pdf_path FROM eccaa_reports WHERE case_id='J8-SXY-2010-08-05'"
    ).fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert os.path.exists(row["pdf_path"])


def test_fetch_failure_keeps_row_new(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "J8-SXY-2010-08-05", "https://www.eccaa.aero/far/c402.pdf")
    monkeypatch.setattr(
        eccaa, "download",
        lambda c, u, d: (_ for _ in ()).throw(RuntimeError("000")),
    )
    monkeypatch.setattr(eccaa, "DELAY", 0)
    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    assert conn.execute(
        "SELECT status FROM eccaa_reports WHERE case_id='J8-SXY-2010-08-05'"
    ).fetchone()["status"] == db.STATUS_NEW


def test_fetch_isolates_per_row_errors(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "J8-SXY-2010-08-05", "https://www.eccaa.aero/far/a.pdf")
    _seed_new(conn, "N8862F-2017-01-12", "https://www.eccaa.aero/far/b.pdf")

    def _sel(client, url, dest):
        if url.endswith("a.pdf"):
            raise RuntimeError("403")
        open(dest, "wb").write(b"%PDF")
    monkeypatch.setattr(eccaa, "download", _sel)
    monkeypatch.setattr(eccaa, "DELAY", 0)

    assert pipeline.fetch(conn, None, str(tmp_path)) == 2
    assert conn.execute(
        "SELECT status FROM eccaa_reports WHERE case_id='J8-SXY-2010-08-05'"
    ).fetchone()["status"] == db.STATUS_NEW
    assert conn.execute(
        "SELECT status FROM eccaa_reports WHERE case_id='N8862F-2017-01-12'"
    ).fetchone()["status"] == db.STATUS_FETCHED


# ── parse (scanned-aware) ───────────────────────────────────────────────────

def _seed_fetched(conn, case_id, pdf_path=None):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO eccaa_reports (case_id, status, pdf_path, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, db.STATUS_FETCHED, pdf_path, ts, ts),
    )
    conn.commit()


def test_parse_long_narrative(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "J8-SXY-2010-08-05", pdf_path="c402.pdf")
    long_text = "X" * MIN_NARRATIVE
    monkeypatch.setattr(pipeline, "extract_text", lambda p: long_text)
    assert pipeline.parse(conn) == 1
    row = conn.execute(
        "SELECT narrative_text, source_tier, status FROM eccaa_reports "
        "WHERE case_id='J8-SXY-2010-08-05'"
    ).fetchone()
    assert row["status"] == db.STATUS_PARSED
    assert row["source_tier"] == "pdf"


def test_parse_scanned_short_tier(monkeypatch):
    """Scanned report (tiny text layer) -> 'short' and is skipped by build."""
    conn = _conn()
    _seed_fetched(conn, "SCAN-1", pdf_path="scan.pdf")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "X" * 40)  # < floor
    assert pipeline.parse(conn) == 1
    row = conn.execute(
        "SELECT source_tier FROM eccaa_reports WHERE case_id='SCAN-1'"
    ).fetchone()
    assert row["source_tier"] == "short"


def test_parse_no_text_tier_none(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "SCAN-2", pdf_path="scan.pdf")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "")
    pipeline.parse(conn)
    assert conn.execute(
        "SELECT source_tier FROM eccaa_reports WHERE case_id='SCAN-2'"
    ).fetchone()["source_tier"] == "none"


# ── build (per-report country) ──────────────────────────────────────────────

def _seed_parsed(conn, case_id, *, aircraft=None, registration=None, location=None,
                 date=None, narrative="", event_class="Accident", country=None,
                 pdf_url=None, report_url=None):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO eccaa_reports "
        "(case_id, aircraft, registration, location, date_of_occurrence, country, "
        "narrative_text, event_class, pdf_url, report_url, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (case_id, aircraft, registration, location, date, country,
         narrative, event_class, pdf_url, report_url, db.STATUS_PARSED, ts, ts),
    )
    conn.commit()


def test_build_uses_per_report_country():
    conn = _conn()
    _seed_parsed(conn, "J8-SXY-2010-08-05", aircraft="Cessna 402-C",
                 registration="J8-SXY", date="2010-08-05", narrative="N" * 700,
                 country="VC", pdf_url="https://www.eccaa.aero/far/c402.pdf")
    _seed_parsed(conn, "N8862F-2017-01-12", aircraft="PA-28-151",
                 registration="N8862F", date="2017-01-12", narrative="N" * 700,
                 country="US", pdf_url="https://www.eccaa.aero/far/pa28.pdf")

    assert pipeline.build(conn) == 2
    rows = {r["case_id"]: r for r in conn.execute(
        "SELECT case_id, country, site_slug, report_type, source_url FROM eccaa_accidents"
    )}
    assert rows["J8-SXY-2010-08-05"]["country"] == "VC"
    assert rows["N8862F-2017-01-12"]["country"] == "US"
    assert rows["J8-SXY-2010-08-05"]["site_slug"].startswith("crash-")
    assert rows["J8-SXY-2010-08-05"]["report_type"] == "Accident"
    assert rows["J8-SXY-2010-08-05"]["source_url"].endswith("c402.pdf")


def test_build_country_fallback_when_null():
    conn = _conn()
    _seed_parsed(conn, "X-1", narrative="N" * (pipeline._NARRATIVE_FLOOR + 100), country=None)
    pipeline.build(conn)
    assert conn.execute(
        "SELECT country FROM eccaa_accidents WHERE case_id='X-1'"
    ).fetchone()["country"] == "AG"  # DEFAULT_COUNTRY


def test_build_skips_scanned_below_floor():
    conn = _conn()
    _seed_parsed(conn, "SCAN-1", narrative="X" * (pipeline._NARRATIVE_FLOOR - 1), country="VC")
    assert pipeline.build(conn) == 0
    assert conn.execute(
        "SELECT status FROM eccaa_reports WHERE case_id='SCAN-1'"
    ).fetchone()["status"] == db.STATUS_SKIPPED
    assert conn.execute("SELECT COUNT(*) FROM eccaa_accidents").fetchone()[0] == 0


def test_build_source_url_falls_back_to_report_url():
    conn = _conn()
    _seed_parsed(conn, "X-2", narrative="N" * (pipeline._NARRATIVE_FLOOR + 100), country="LC",
                 pdf_url=None, report_url=eccaa.INDEX_URL)
    pipeline.build(conn)
    assert conn.execute(
        "SELECT source_url FROM eccaa_accidents WHERE case_id='X-2'"
    ).fetchone()["source_url"] == eccaa.INDEX_URL
