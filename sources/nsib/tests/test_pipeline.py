import os

from nsib_ingest import nsib, db, pipeline
from nsib_ingest.pdf import MIN_NARRATIVE


def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


# Fake listing rows: row0 prelim+pdf, row1 interim+structured-ref pdf,
# row2 final NO pdf (skipped at discover), row3 garbled reg cell + pdf.
_FAKE_ROWS = [
    {"date": "2025-09-14", "post_url": "https://nsib.gov.ng/preliminary-report-air-peace-limited-5n-bqq/",
     "title": "preliminary-report Air Peace Limited 5N-BQQ", "operator": "Air Peace Limited",
     "category": "Accident Report", "event_class": "Serious Incident", "report_type_col": "Air Accident Report",
     "reg_cell": "5N-BQQ", "status": "Preliminary Report",
     "pdf_url": "https://nsib.gov.ng/wp-content/uploads/ninja-forms/3/Preliminary-Report-18.pdf"},
    {"date": "2025-12-14", "post_url": "https://nsib.gov.ng/interim-statement-allied-air-limited-5n-jrt/",
     "title": "interim-statement Allied Air Limited 5N-JRT", "operator": "Allied Air Limited",
     "category": "Accident Report", "event_class": "Accident", "report_type_col": "Air Accident Report",
     "reg_cell": "5N-JRT", "status": "Interim Statement",
     "pdf_url": "https://nsib.gov.ng/wp-content/uploads/ninja-forms/3/AAL/2024/12/11/INTR/01.pdf"},
    {"date": "2013-12-04", "post_url": "https://nsib.gov.ng/final-report-veteran-avia/",
     "title": "Final-Report Veteran Avia", "operator": "Veteran Avia",
     "category": "Accident Report", "event_class": "Accident", "report_type_col": "Air Accident Report",
     "reg_cell": "EK-74798", "status": "Final Report", "pdf_url": None},
    {"date": "2023-06-15", "post_url": "https://nsib.gov.ng/interim-statement-overland-airways-limited/",
     "title": "interim-statement Overland Airways Limited", "operator": "Overland Airways Limited",
     "category": "Accident Report", "event_class": "Accident", "report_type_col": "Air Accident Report",
     "reg_cell": "Limited", "status": "Interim Statement",
     "pdf_url": "https://nsib.gov.ng/wp-content/uploads/2024/04/INTERIM-STATEMENT-01-5N-BRQOVERLAND.pdf"},
]


class _FakeResp:
    def __init__(self, body=""):
        self.text = body
        self.content = body.encode() if isinstance(body, str) else body
        self.status_code = 200

    def raise_for_status(self):
        pass


class _FakeClient:
    def __init__(self):
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        return _FakeResp("<index/>")


# ── discover ────────────────────────────────────────────────────────────────

def test_discover_inserts_only_rows_with_pdf(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(nsib, "iter_page_urls", lambda html: [nsib.INDEX_URL])
    monkeypatch.setattr(nsib, "parse_listing", lambda html: _FAKE_ROWS)
    monkeypatch.setattr(nsib, "DELAY", 0)

    # 3 of 4 rows have a pdf (final has none -> not inserted)
    assert pipeline.discover(conn, _FakeClient()) == 3

    rows = conn.execute(
        "SELECT case_id, registration, date_of_occurrence, report_type, status "
        "FROM nsib_reports ORDER BY case_id"
    ).fetchall()
    cids = {r["case_id"] for r in rows}
    assert "NSIB-PRE-5N-BQQ-2025-09-14" in cids       # reg from cell + date
    assert "AAL/2024/12/11/INTR/01" in cids           # structured ref preferred
    assert "NSIB-INT-5N-BRQ-2023-06-15" in cids       # reg from filename (garbled cell)
    # final report (no pdf) absent
    assert not any("veteran" in (r["case_id"] or "").lower() for r in rows)
    assert all(r["status"] == db.STATUS_NEW for r in rows)


def test_discover_skips_blank_form_rows(monkeypatch):
    conn = _conn()
    form_row = {
        "date": "2007-02-07", "post_url": "https://nsib.gov.ng/accident-report-form-001/",
        "title": "Accident Report Form 001", "operator": None, "category": None,
        "event_class": None, "report_type_col": None, "reg_cell": "001",
        "status": "Report",  # NOT a real investigation report
        "pdf_url": "https://nsib.gov.ng/wp-content/uploads/2024/04/Accident-Incident-Report-Form-NSIB-Form-001.pdf",
    }
    monkeypatch.setattr(nsib, "iter_page_urls", lambda html: [nsib.INDEX_URL])
    monkeypatch.setattr(nsib, "parse_listing", lambda html: _FAKE_ROWS + [form_row])
    monkeypatch.setattr(nsib, "DELAY", 0)
    # still 3 (final has no pdf; form row has non-report status)
    assert pipeline.discover(conn, _FakeClient()) == 3
    assert not conn.execute(
        "SELECT 1 FROM nsib_reports WHERE case_id LIKE '%2007%'"
    ).fetchone()


def test_discover_idempotent(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(nsib, "iter_page_urls", lambda html: [nsib.INDEX_URL])
    monkeypatch.setattr(nsib, "parse_listing", lambda html: _FAKE_ROWS)
    monkeypatch.setattr(nsib, "DELAY", 0)
    assert pipeline.discover(conn, _FakeClient()) == 3
    assert pipeline.discover(conn, _FakeClient()) == 0
    assert conn.execute("SELECT COUNT(*) FROM nsib_reports").fetchone()[0] == 3


def test_discover_case_id_is_intrinsic_not_url(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(nsib, "iter_page_urls", lambda html: [nsib.INDEX_URL])
    monkeypatch.setattr(nsib, "parse_listing", lambda html: _FAKE_ROWS)
    monkeypatch.setattr(nsib, "DELAY", 0)
    pipeline.discover(conn, _FakeClient())
    for (cid,) in conn.execute("SELECT case_id FROM nsib_reports"):
        assert "http" not in cid
        assert "/air-reports" not in cid


# ── fetch ─────────────────────────────────────────────────────────────────────

def _seed_new(conn, case_id, pdf_url):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO nsib_reports (case_id, pdf_url, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, pdf_url, db.STATUS_NEW, ts, ts),
    )
    conn.commit()


def test_fetch_downloads_and_advances(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "AAL/2024/12/11/INTR/01", "https://nsib.gov.ng/x.pdf")
    calls = []

    def fake_dl(client, url, dest):
        calls.append((url, dest))
        open(dest, "wb").write(b"%PDF")

    monkeypatch.setattr(nsib, "download", fake_dl)
    monkeypatch.setattr(nsib, "DELAY", 0)

    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    row = conn.execute(
        "SELECT status, pdf_path FROM nsib_reports WHERE case_id='AAL/2024/12/11/INTR/01'"
    ).fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert os.path.exists(row["pdf_path"])
    # slash in case_id must be sanitised in the filename
    assert "/" not in os.path.basename(row["pdf_path"]).replace(".pdf", "")
    assert len(calls) == 1


def test_fetch_download_failure_stays_new(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "NSIB-PRE-5N-BQQ-2025-09-14", "https://nsib.gov.ng/x.pdf")

    def boom(*a, **k):
        raise RuntimeError("403")

    monkeypatch.setattr(nsib, "download", boom)
    monkeypatch.setattr(nsib, "DELAY", 0)

    pipeline.fetch(conn, None, str(tmp_path))
    row = conn.execute(
        "SELECT status FROM nsib_reports WHERE case_id='NSIB-PRE-5N-BQQ-2025-09-14'"
    ).fetchone()
    assert row["status"] == db.STATUS_NEW  # retry next run


# ── parse ─────────────────────────────────────────────────────────────────────

def _seed_fetched(conn, case_id, pdf_path):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO nsib_reports (case_id, pdf_path, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, pdf_path, db.STATUS_FETCHED, ts, ts),
    )
    conn.commit()


def test_parse_pdf_tier(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "C1", "/tmp/a.pdf")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "X" * (MIN_NARRATIVE + 10))
    assert pipeline.parse(conn, enable_ocr=False) == 1
    row = conn.execute("SELECT source_tier, status, narrative_text FROM nsib_reports WHERE case_id='C1'").fetchone()
    assert row["source_tier"] == "pdf"
    assert row["status"] == db.STATUS_PARSED
    assert len(row["narrative_text"]) >= MIN_NARRATIVE


def test_parse_ocr_fallback(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "C2", "/tmp/scan.pdf")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "")  # empty text layer
    monkeypatch.setattr(pipeline, "ocr_extract", lambda p, lang="eng": "Y" * (MIN_NARRATIVE + 5))
    assert pipeline.parse(conn, enable_ocr=True) == 1
    row = conn.execute("SELECT source_tier FROM nsib_reports WHERE case_id='C2'").fetchone()
    assert row["source_tier"] == "ocr"


def test_parse_ocr_disabled_yields_none(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "C3", "/tmp/scan.pdf")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "")
    # ocr must NOT be called when disabled
    monkeypatch.setattr(pipeline, "ocr_extract", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ocr called")))
    assert pipeline.parse(conn, enable_ocr=False) == 1
    row = conn.execute("SELECT source_tier FROM nsib_reports WHERE case_id='C3'").fetchone()
    assert row["source_tier"] == "none"


# ── build ─────────────────────────────────────────────────────────────────────

def _seed_parsed(conn, case_id, narrative, **extra):
    ts = db.now_ms()
    cols = dict(case_id=case_id, narrative_text=narrative, status=db.STATUS_PARSED,
                discovered_at=ts, updated_at=ts,
                date_of_occurrence="2025-09-14", registration="5N-BQQ",
                pdf_url="https://nsib.gov.ng/x.pdf", report_type="Preliminary Report")
    cols.update(extra)
    keys = ",".join(cols)
    qs = ",".join("?" * len(cols))
    conn.execute(f"INSERT INTO nsib_reports ({keys}) VALUES ({qs})", tuple(cols.values()))
    conn.commit()


def test_build_emits_accident(monkeypatch):
    conn = _conn()
    _seed_parsed(conn, "NSIB-PRE-5N-BQQ-2025-09-14", "N" * 500)
    assert pipeline.build(conn) == 1
    row = conn.execute("SELECT * FROM nsib_accidents WHERE case_id='NSIB-PRE-5N-BQQ-2025-09-14'").fetchone()
    assert row["country"] == "NG"
    assert row["event_date"] == "2025-09-14"
    assert row["registration"] == "5N-BQQ"
    assert row["source_url"] == "https://nsib.gov.ng/x.pdf"
    assert row["site_slug"].startswith("crash-")
    rep = conn.execute("SELECT status FROM nsib_reports WHERE case_id='NSIB-PRE-5N-BQQ-2025-09-14'").fetchone()
    assert rep["status"] == db.STATUS_BUILT


def test_build_skips_thin_narrative(monkeypatch):
    conn = _conn()
    _seed_parsed(conn, "THIN", "tiny")
    assert pipeline.build(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM nsib_accidents").fetchone()[0] == 0
    rep = conn.execute("SELECT status FROM nsib_reports WHERE case_id='THIN'").fetchone()
    assert rep["status"] == db.STATUS_SKIPPED
