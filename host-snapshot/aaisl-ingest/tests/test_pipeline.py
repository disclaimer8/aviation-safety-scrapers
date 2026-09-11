# tests/test_pipeline.py
"""Pipeline tests for aaisl discover -> fetch -> parse -> build."""
import os

from aaisl_ingest import aaisl, db, pipeline
from aaisl_ingest.pdf import MIN_NARRATIVE


def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


_FAKE_ROWS = [
    {
        "case_id": "4R-CAE-20260107",
        "report_url": None,
        "pdf_url_es": None,
        "pdf_url_en": "https://www.caa.lk/images/pdf/2026_May/r1.pdf",
        "event_class": "Accident",
        "aircraft": "Cessna 208",
        "registration": "4R-CAE",
        "date_of_occurrence": "2026-01-07",
        "location": "Gregory Lake",
        "operator": "Saffron Aviation (Pvt) Ltd",
        "title": "Final Report - Accident involving Cessna 208 4R-CAE",
    },
    {
        "case_id": "4R-ABN-20190321",  # the TSIB-rehost row
        "report_url": None,
        "pdf_url_es": None,
        "pdf_url_en": "https://www.caa.lk/images/pdf/accident_investigation_unit/Accident_Reports_New/18_4R-ABN.pdf",
        "event_class": "Incident",
        "aircraft": "Airbus A320",
        "registration": "4R-ABN",
        "date_of_occurrence": "2019-03-21",
        "location": "Changi International Airport, Singapore",
        "operator": "Sri Lankan Airlines",
        "title": "Airbus A320 4R-ABN Damage to Runway Edge Lights",
    },
]


class _FakeResp:
    def __init__(self, body=""):
        self.content = body.encode("utf-8") if isinstance(body, str) else body
        self.status_code = 200

    def raise_for_status(self):
        pass


class _FakeClient:
    def __init__(self, index_html="<index>"):
        self._index_html = index_html
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return _FakeResp(self._index_html)


# ── discover ────────────────────────────────────────────────────────────────

def test_discover_inserts_new_rows(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(aaisl, "parse_listing", lambda html, base_url=aaisl.BASE: _FAKE_ROWS)
    client = _FakeClient()

    assert pipeline.discover(conn, client) == 2

    rows = conn.execute(
        "SELECT case_id, pdf_url, pdf_url_en, pdf_url_es, lang, status, event_class, "
        "aircraft, registration, date_of_occurrence, location, operator "
        "FROM aaisl_reports ORDER BY case_id"
    ).fetchall()
    assert len(rows) == 2
    assert all(r["status"] == db.STATUS_NEW for r in rows)

    r = next(x for x in rows if x["case_id"] == "4R-CAE-20260107")
    assert r["pdf_url"] == "https://www.caa.lk/images/pdf/2026_May/r1.pdf"
    assert r["pdf_url_en"] == r["pdf_url"]
    assert r["pdf_url_es"] is None
    assert r["lang"] == "en"
    assert r["event_class"] == "Accident"
    assert r["registration"] == "4R-CAE"
    assert r["date_of_occurrence"] == "2026-01-07"
    assert r["operator"] == "Saffron Aviation (Pvt) Ltd"


def test_discover_idempotent(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(aaisl, "parse_listing", lambda html, base_url=aaisl.BASE: _FAKE_ROWS)
    client = _FakeClient()
    assert pipeline.discover(conn, client) == 2
    assert pipeline.discover(conn, client) == 0
    assert conn.execute("SELECT COUNT(*) FROM aaisl_reports").fetchone()[0] == 2


def test_discover_full_flag_accepted(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(aaisl, "parse_listing", lambda html, base_url=aaisl.BASE: _FAKE_ROWS)
    assert pipeline.discover(conn, _FakeClient(), full=True) == 2


# ── fetch ─────────────────────────────────────────────────────────────────

def _seed_new(conn, case_id, pdf_url):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO aaisl_reports (case_id, pdf_url, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, pdf_url, db.STATUS_NEW, ts, ts),
    )
    conn.commit()


def test_fetch_downloads_pdf_and_advances(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "4R-CAE-20260107", "https://www.caa.lk/r1.pdf")

    calls = []
    def _fake_download(client, url, dest):
        calls.append((url, dest))
        open(dest, "wb").write(b"%PDF")

    monkeypatch.setattr(aaisl, "download", _fake_download)
    monkeypatch.setattr(aaisl, "DELAY", 0)

    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    row = conn.execute(
        "SELECT status, pdf_path FROM aaisl_reports WHERE case_id='4R-CAE-20260107'"
    ).fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert os.path.exists(row["pdf_path"])
    assert len(calls) == 1


def test_fetch_download_failure_keeps_row_new(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "4R-CAE-20260107", "https://www.caa.lk/r1.pdf")
    monkeypatch.setattr(
        aaisl, "download",
        lambda c, u, d: (_ for _ in ()).throw(RuntimeError("timeout")),
    )
    monkeypatch.setattr(aaisl, "DELAY", 0)
    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    assert conn.execute(
        "SELECT status FROM aaisl_reports WHERE case_id='4R-CAE-20260107'"
    ).fetchone()["status"] == db.STATUS_NEW


def test_fetch_isolates_per_row_errors(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "4R-AAA-20200101", "https://www.caa.lk/a.pdf")
    _seed_new(conn, "4R-BBB-20200202", "https://www.caa.lk/b.pdf")

    def _sel(client, url, dest):
        if "/a.pdf" in url:
            raise RuntimeError("403")
        open(dest, "wb").write(b"%PDF")

    monkeypatch.setattr(aaisl, "download", _sel)
    monkeypatch.setattr(aaisl, "DELAY", 0)
    assert pipeline.fetch(conn, None, str(tmp_path)) == 2
    assert conn.execute("SELECT status FROM aaisl_reports WHERE case_id='4R-AAA-20200101'").fetchone()["status"] == db.STATUS_NEW
    assert conn.execute("SELECT status FROM aaisl_reports WHERE case_id='4R-BBB-20200202'").fetchone()["status"] == db.STATUS_FETCHED


# ── parse ─────────────────────────────────────────────────────────────────

def _seed_fetched(conn, case_id, pdf_path="x.pdf"):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO aaisl_reports (case_id, status, pdf_path, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, db.STATUS_FETCHED, pdf_path, ts, ts),
    )
    conn.commit()


def test_parse_long_narrative(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "4R-CAE-20260107")
    long_text = "X" * MIN_NARRATIVE
    monkeypatch.setattr(pipeline, "extract_text", lambda p: long_text)
    assert pipeline.parse(conn) == 1
    row = conn.execute(
        "SELECT narrative_text, source_tier, status FROM aaisl_reports WHERE case_id='4R-CAE-20260107'"
    ).fetchone()
    assert row["status"] == db.STATUS_PARSED
    assert row["source_tier"] == "pdf"
    assert row["narrative_text"] == long_text


def test_parse_foreign_authority_skipped(monkeypatch):
    """A re-hosted TSIB report is detected and SKIPPED with tier='foreign'."""
    conn = _conn()
    _seed_fetched(conn, "4R-ABN-20190321")
    tsib_text = (
        "Final Report 4R-ABN\n"
        "Transport Safety Investigation Bureau\nMinistry of Transport\nSingapore\n"
        + ("detail " * 200)
    )
    monkeypatch.setattr(pipeline, "extract_text", lambda p: tsib_text)
    assert pipeline.parse(conn) == 1
    row = conn.execute(
        "SELECT source_tier, status FROM aaisl_reports WHERE case_id='4R-ABN-20190321'"
    ).fetchone()
    assert row["source_tier"] == "foreign"
    assert row["status"] == db.STATUS_SKIPPED


def test_parse_sri_lanka_report_not_skipped(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "4R-CAE-20260107")
    sl_text = "Released by the Civil Aviation Authority of Sri Lanka\n" + ("body " * 200)
    monkeypatch.setattr(pipeline, "extract_text", lambda p: sl_text)
    pipeline.parse(conn)
    row = conn.execute(
        "SELECT source_tier, status FROM aaisl_reports WHERE case_id='4R-CAE-20260107'"
    ).fetchone()
    assert row["status"] == db.STATUS_PARSED
    assert row["source_tier"] == "pdf"


def test_parse_scanned_below_floor(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "4R-OLD-19741204")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "X" * 100)
    pipeline.parse(conn)
    row = conn.execute(
        "SELECT source_tier, status FROM aaisl_reports WHERE case_id='4R-OLD-19741204'"
    ).fetchone()
    assert row["source_tier"] == "scanned"
    assert row["status"] == db.STATUS_PARSED


def test_parse_no_text_skipped(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "4R-CAE-20260107", pdf_path=None)
    pipeline.parse(conn)
    row = conn.execute(
        "SELECT source_tier, status FROM aaisl_reports WHERE case_id='4R-CAE-20260107'"
    ).fetchone()
    assert row["source_tier"] == "none"
    assert row["status"] == db.STATUS_SKIPPED


# ── build ─────────────────────────────────────────────────────────────────

def _seed_parsed(conn, case_id, *, aircraft=None, registration=None, location=None,
                 date=None, narrative="", event_class=None, operator=None,
                 source_tier="pdf", pdf_url=None, report_url=None):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO aaisl_reports "
        "(case_id, aircraft, registration, location, date_of_occurrence, narrative_text, "
        "event_class, operator, source_tier, pdf_url, report_url, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (case_id, aircraft, registration, location, date, narrative, event_class,
         operator, source_tier, pdf_url, report_url, db.STATUS_PARSED, ts, ts),
    )
    conn.commit()


def test_build_creates_accident_row():
    conn = _conn()
    narr = "N" * 700
    _seed_parsed(conn, "4R-CAE-20260107", aircraft="Cessna 208", registration="4R-CAE",
                 location="Gregory Lake", date="2026-01-07", narrative=narr,
                 event_class="Accident", operator="Saffron Aviation",
                 pdf_url="https://www.caa.lk/r1.pdf")
    assert pipeline.build(conn) == 1
    acc = conn.execute("SELECT * FROM aaisl_accidents WHERE case_id='4R-CAE-20260107'").fetchone()
    assert acc["country"] == "LK"
    assert acc["event_date"] == "2026-01-07"
    assert acc["aircraft"] == "Cessna 208"
    assert acc["registration"] == "4R-CAE"
    assert acc["operator"] == "Saffron Aviation"
    assert acc["report_type"] == "Accident"
    assert acc["source_url"] == "https://www.caa.lk/r1.pdf"
    assert acc["site_slug"].startswith("crash-")
    assert conn.execute(
        "SELECT status FROM aaisl_reports WHERE case_id='4R-CAE-20260107'"
    ).fetchone()["status"] == db.STATUS_BUILT


def test_build_skips_scanned():
    conn = _conn()
    _seed_parsed(conn, "4R-OLD-19741204", narrative="X" * 100, source_tier="scanned",
                 event_class="Accident")
    assert pipeline.build(conn) == 0
    assert conn.execute(
        "SELECT status FROM aaisl_reports WHERE case_id='4R-OLD-19741204'"
    ).fetchone()["status"] == db.STATUS_SKIPPED
    assert conn.execute("SELECT COUNT(*) FROM aaisl_accidents").fetchone()[0] == 0


def test_build_skips_below_narrative_floor():
    conn = _conn()
    _seed_parsed(conn, "4R-X-20240101", narrative="X" * 79, source_tier="short",
                 event_class="Incident")
    assert pipeline.build(conn) == 0
    assert conn.execute(
        "SELECT status FROM aaisl_reports WHERE case_id='4R-X-20240101'"
    ).fetchone()["status"] == db.STATUS_SKIPPED


def test_build_country_is_lk():
    conn = _conn()
    _seed_parsed(conn, "4R-CAE-20260107", narrative="N" * 200, event_class="Accident")
    pipeline.build(conn)
    assert conn.execute(
        "SELECT country FROM aaisl_accidents WHERE case_id='4R-CAE-20260107'"
    ).fetchone()["country"] == "LK"


def test_build_source_url_falls_back_to_report_url():
    conn = _conn()
    _seed_parsed(conn, "LK-FOO", narrative="N" * 200, event_class="Accident",
                 pdf_url=None, report_url="https://www.caa.lk/x")
    pipeline.build(conn)
    assert conn.execute(
        "SELECT source_url FROM aaisl_accidents WHERE case_id='LK-FOO'"
    ).fetchone()["source_url"] == "https://www.caa.lk/x"
