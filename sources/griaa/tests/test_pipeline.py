# tests/test_pipeline.py
"""Pipeline tests for griaa discover → fetch → parse → build."""
import os

from griaa_ingest import griaa, db, pipeline
from griaa_ingest.pdf import SCANNED_THRESHOLD


def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


_FAKE_ROWS = [
    {
        "case_id": "COL-08-31-GIA",
        "report_url": None,
        "pdf_url_es": "https://www.aerocivil.gov.co/media/col-08-31-gia%20final.pdf",
        "pdf_url_en": None,
        "event_class": "Accidente",
        "aircraft": "L-410UVP-E",
        "registration": "HK4235",
        "date_of_occurrence": "2008-12-12",
        "location": "Acandí",
        "title": "COL-08-31-GIA",
    },
    {
        "case_id": "COL-24-58-DIACC",
        "report_url": None,
        "pdf_url_es": "https://www.aerocivil.gov.co/media/col-24-58-diacc%20prelim.pdf",
        "pdf_url_en": None,
        "event_class": "Incidente grave",
        "aircraft": "A320",
        "registration": "HK5100",
        "date_of_occurrence": "2024-09-01",
        "location": "Bogotá",
        "title": "COL-24-58-DIACC",
    },
]


class _FakeResp:
    def __init__(self, body=""):
        self.content = body.encode("utf-8") if isinstance(body, str) else body
        self.status_code = 200

    def raise_for_status(self):
        pass


class _FakeClient:
    def __init__(self, body="<html></html>"):
        self._body = body
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return _FakeResp(self._body)


# ── discover ──

def test_discover_inserts_new_rows(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(griaa, "iter_year_urls", lambda *a, **k: [griaa.year_url(2008)])
    monkeypatch.setattr(griaa, "parse_listing", lambda html, url="": _FAKE_ROWS)
    monkeypatch.setattr(griaa, "DELAY", 0)

    assert pipeline.discover(conn, _FakeClient()) == 2

    rows = conn.execute(
        "SELECT case_id, pdf_url, pdf_url_es, lang, status, event_class, "
        "aircraft, registration, date_of_occurrence, location "
        "FROM griaa_reports ORDER BY case_id"
    ).fetchall()
    assert len(rows) == 2
    assert all(r["status"] == db.STATUS_NEW for r in rows)

    g = next(r for r in rows if r["case_id"] == "COL-08-31-GIA")
    assert g["pdf_url"] == "https://www.aerocivil.gov.co/media/col-08-31-gia%20final.pdf"
    assert g["lang"] == "es"
    assert g["event_class"] == "Accidente"
    assert g["aircraft"] == "L-410UVP-E"
    assert g["registration"] == "HK4235"
    assert g["date_of_occurrence"] == "2008-12-12"
    assert g["location"] == "Acandí"


def test_discover_idempotent(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(griaa, "iter_year_urls", lambda *a, **k: [griaa.year_url(2008)])
    monkeypatch.setattr(griaa, "parse_listing", lambda html, url="": _FAKE_ROWS)
    monkeypatch.setattr(griaa, "DELAY", 0)

    assert pipeline.discover(conn, _FakeClient()) == 2
    assert pipeline.discover(conn, _FakeClient()) == 0
    assert conn.execute("SELECT COUNT(*) FROM griaa_reports").fetchone()[0] == 2


def test_discover_skips_existing_case_id(monkeypatch):
    conn = _conn()
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO griaa_reports (case_id, status, discovered_at, updated_at) VALUES (?,?,?,?)",
        ("COL-08-31-GIA", db.STATUS_NEW, ts, ts),
    )
    conn.commit()
    monkeypatch.setattr(griaa, "iter_year_urls", lambda *a, **k: [griaa.year_url(2008)])
    monkeypatch.setattr(griaa, "parse_listing", lambda html, url="": _FAKE_ROWS)
    monkeypatch.setattr(griaa, "DELAY", 0)

    assert pipeline.discover(conn, _FakeClient()) == 1


def test_discover_full_flag_accepted(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(griaa, "iter_year_urls", lambda *a, **k: [griaa.year_url(2008)])
    monkeypatch.setattr(griaa, "parse_listing", lambda html, url="": _FAKE_ROWS)
    monkeypatch.setattr(griaa, "DELAY", 0)
    assert pipeline.discover(conn, _FakeClient(), full=True) == 2


# ── fetch ──

def _seed_new(conn, case_id, pdf_url):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO griaa_reports (case_id, pdf_url, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, pdf_url, db.STATUS_NEW, ts, ts),
    )
    conn.commit()


def test_fetch_downloads_pdf_and_advances(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "COL-08-31-GIA", "https://x/col-08-31.pdf")

    calls = []
    def _fake_download(client, url, dest):
        calls.append((url, dest))
        open(dest, "wb").write(b"%PDF")
    monkeypatch.setattr(griaa, "download", _fake_download)
    monkeypatch.setattr(griaa, "DELAY", 0)

    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    row = conn.execute("SELECT status, pdf_path FROM griaa_reports WHERE case_id='COL-08-31-GIA'").fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert os.path.exists(row["pdf_path"])
    assert len(calls) == 1


def test_fetch_no_pdf_url_advances_with_null_path(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "COL-99-01-GIA", None)
    calls = []
    monkeypatch.setattr(griaa, "download", lambda *a: calls.append(a))
    monkeypatch.setattr(griaa, "DELAY", 0)

    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    assert not calls
    row = conn.execute("SELECT status, pdf_path FROM griaa_reports WHERE case_id='COL-99-01-GIA'").fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert row["pdf_path"] is None


def test_fetch_download_failure_keeps_row_new(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "COL-08-31-GIA", "https://x/a.pdf")
    monkeypatch.setattr(griaa, "download",
                        lambda c, u, d: (_ for _ in ()).throw(RuntimeError("timeout")))
    monkeypatch.setattr(griaa, "DELAY", 0)
    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    assert conn.execute("SELECT status FROM griaa_reports WHERE case_id='COL-08-31-GIA'").fetchone()["status"] == db.STATUS_NEW


def test_fetch_isolates_per_row_errors(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "COL-08-31-GIA", "https://x/a-fail.pdf")
    _seed_new(conn, "COL-24-58-DIACC", "https://x/b-ok.pdf")

    def _sel(client, url, dest):
        if "fail" in url:
            raise RuntimeError("403")
        open(dest, "wb").write(b"%PDF")
    monkeypatch.setattr(griaa, "download", _sel)
    monkeypatch.setattr(griaa, "DELAY", 0)

    assert pipeline.fetch(conn, None, str(tmp_path)) == 2
    assert conn.execute("SELECT status FROM griaa_reports WHERE case_id='COL-08-31-GIA'").fetchone()["status"] == db.STATUS_NEW
    assert conn.execute("SELECT status FROM griaa_reports WHERE case_id='COL-24-58-DIACC'").fetchone()["status"] == db.STATUS_FETCHED


# ── parse ──

def _seed_fetched(conn, case_id, pdf_path=None):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO griaa_reports (case_id, status, pdf_path, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, db.STATUS_FETCHED, pdf_path, ts, ts),
    )
    conn.commit()


def test_parse_long_narrative_pdf_tier(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "COL-08-31-GIA", pdf_path="a.pdf")
    long_text = "ADVERTENCIA\nlegal stuff\nSINOPSIS\n" + ("X" * SCANNED_THRESHOLD)
    monkeypatch.setattr(pipeline, "extract_text", lambda p: long_text)

    assert pipeline.parse(conn) == 1
    row = conn.execute("SELECT narrative_text, source_tier, status FROM griaa_reports WHERE case_id='COL-08-31-GIA'").fetchone()
    assert row["status"] == db.STATUS_PARSED
    assert row["source_tier"] == "pdf"
    assert "ADVERTENCIA" not in row["narrative_text"]  # preamble stripped


def test_parse_scanned_pdf_tier(monkeypatch):
    """Image-only scan: a few chars of text → tier='scanned'."""
    conn = _conn()
    _seed_fetched(conn, "COL-08-31-GIA", pdf_path="a.pdf")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "  \x0c  3  ")  # trivial
    assert pipeline.parse(conn) == 1
    row = conn.execute("SELECT source_tier FROM griaa_reports WHERE case_id='COL-08-31-GIA'").fetchone()
    assert row["source_tier"] == "scanned"


def test_parse_no_pdf_path_tier_none(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "COL-99-01-GIA", pdf_path=None)
    calls = []
    monkeypatch.setattr(pipeline, "extract_text", lambda p: calls.append(p) or "X" * 1000)
    assert pipeline.parse(conn) == 1
    assert not calls
    row = conn.execute("SELECT source_tier, narrative_text FROM griaa_reports WHERE case_id='COL-99-01-GIA'").fetchone()
    assert row["source_tier"] == "none"
    assert row["narrative_text"] == ""


def test_parse_empty_extraction_tier_none(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "COL-08-31-GIA", pdf_path="a.pdf")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "")
    pipeline.parse(conn)
    assert conn.execute("SELECT source_tier FROM griaa_reports WHERE case_id='COL-08-31-GIA'").fetchone()["source_tier"] == "none"


# ── build ──

def _seed_parsed(conn, case_id, *, aircraft=None, registration=None, location=None,
                 date=None, narrative="", event_class=None, tier="pdf",
                 pdf_url=None, report_url=None):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO griaa_reports "
        "(case_id, aircraft, registration, location, date_of_occurrence, "
        "narrative_text, event_class, source_tier, pdf_url, report_url, "
        "status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (case_id, aircraft, registration, location, date, narrative, event_class,
         tier, pdf_url, report_url, db.STATUS_PARSED, ts, ts),
    )
    conn.commit()


def test_build_creates_accident_row():
    conn = _conn()
    narr = "N" * 200
    _seed_parsed(conn, "COL-08-31-GIA", aircraft="L-410UVP-E", registration="HK4235",
                 location="Acandí", date="2008-12-12", narrative=narr,
                 event_class="Accidente", tier="pdf",
                 pdf_url="https://www.aerocivil.gov.co/media/x.pdf")

    assert pipeline.build(conn) == 1
    acc = conn.execute("SELECT * FROM griaa_accidents WHERE case_id='COL-08-31-GIA'").fetchone()
    assert acc["country"] == "CO"
    assert acc["event_date"] == "2008-12-12"
    assert acc["aircraft"] == "L-410UVP-E"
    assert acc["registration"] == "HK4235"
    assert acc["report_type"] == "Accidente"
    assert acc["narrative_text"] == narr
    assert acc["probable_cause"] is None
    assert acc["source_url"] == "https://www.aerocivil.gov.co/media/x.pdf"
    assert acc["site_slug"] == "col-08-31-gia"
    assert conn.execute("SELECT status FROM griaa_reports WHERE case_id='COL-08-31-GIA'").fetchone()["status"] == db.STATUS_BUILT


def test_build_skips_scanned_tier():
    conn = _conn()
    _seed_parsed(conn, "COL-08-31-GIA", narrative="short scan text", tier="scanned",
                 event_class="Accidente")
    assert pipeline.build(conn) == 0
    assert conn.execute("SELECT status FROM griaa_reports WHERE case_id='COL-08-31-GIA'").fetchone()["status"] == db.STATUS_SKIPPED
    assert conn.execute("SELECT COUNT(*) FROM griaa_accidents").fetchone()[0] == 0


def test_build_skips_none_tier():
    conn = _conn()
    _seed_parsed(conn, "COL-99-01-GIA", narrative="", tier="none", event_class="Accidente")
    assert pipeline.build(conn) == 0
    assert conn.execute("SELECT status FROM griaa_reports WHERE case_id='COL-99-01-GIA'").fetchone()["status"] == db.STATUS_SKIPPED


def test_build_skips_below_narrative_floor():
    conn = _conn()
    _seed_parsed(conn, "COL-08-02-GIA", narrative="X" * 79, tier="pdf",
                 event_class="Accidente")
    assert pipeline.build(conn) == 0
    assert conn.execute("SELECT status FROM griaa_reports WHERE case_id='COL-08-02-GIA'").fetchone()["status"] == db.STATUS_SKIPPED


def test_build_source_url_falls_back_to_report_url():
    conn = _conn()
    _seed_parsed(conn, "COL-08-31-GIA", narrative="N" * 200, tier="pdf",
                 event_class="Accidente", pdf_url=None,
                 report_url="https://www.aerocivil.gov.co/r")
    pipeline.build(conn)
    assert conn.execute("SELECT source_url FROM griaa_accidents WHERE case_id='COL-08-31-GIA'").fetchone()["source_url"] == "https://www.aerocivil.gov.co/r"


def test_build_country_is_co():
    conn = _conn()
    _seed_parsed(conn, "COL-08-31-GIA", narrative="N" * 200, tier="pdf", event_class="Accidente")
    pipeline.build(conn)
    assert conn.execute("SELECT country FROM griaa_accidents WHERE case_id='COL-08-31-GIA'").fetchone()["country"] == "CO"


def test_build_mixed_rows():
    conn = _conn()
    narr = "Z" * 200
    _seed_parsed(conn, "COL-08-01-GIA", narrative=narr, tier="pdf", event_class="Accidente")
    _seed_parsed(conn, "COL-08-02-GIA", narrative=narr, tier="pdf", event_class="Incidente grave")
    _seed_parsed(conn, "COL-08-03-GIA", narrative="x" * 200, tier="scanned", event_class="Accidente")
    assert pipeline.build(conn) == 2
    assert conn.execute("SELECT COUNT(*) FROM griaa_accidents").fetchone()[0] == 2
    assert conn.execute("SELECT status FROM griaa_reports WHERE case_id='COL-08-03-GIA'").fetchone()["status"] == db.STATUS_SKIPPED
