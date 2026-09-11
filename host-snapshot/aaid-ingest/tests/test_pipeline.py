# tests/test_pipeline.py
"""Pipeline tests for aaid discover -> fetch -> parse -> build."""
import os

from aaid_ingest import aaid, db, pipeline
from aaid_ingest.pdf import MIN_NARRATIVE


def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


_FAKE_ROWS = [
    {
        "case_id": "5Y-LOL-2024-12-07",
        "report_url": aaid.INDEX_URL,
        "pdf_url": "https://aaid.transport.go.ke/sites/default/files/2026-06/Final%20Report-5Y-LOL.pdf",
        "pdf_url_es": None,
        "pdf_url_en": "https://aaid.transport.go.ke/sites/default/files/2026-06/Final%20Report-5Y-LOL.pdf",
        "event_class": "Accident",
        "aircraft": None,
        "registration": "5Y-LOL",
        "date_of_occurrence": "2024-12-07",
        "location": None,
        "title": "Final Report 5Y-LOL",
        "lang": "en",
    },
    {
        "case_id": "ZS-OYI-2010-05-01",
        "report_url": aaid.INDEX_URL,
        "pdf_url": "https://aaid.transport.go.ke/sites/default/files/x/Final%20Report-ZS-OYI.pdf",
        "pdf_url_es": None,
        "pdf_url_en": "https://aaid.transport.go.ke/sites/default/files/x/Final%20Report-ZS-OYI.pdf",
        "event_class": "Accident",
        "aircraft": None,
        "registration": "ZS-OYI",
        "date_of_occurrence": "2010-05-01",
        "location": None,
        "title": "Final Report ZS-OYI",
        "lang": "en",
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
    monkeypatch.setattr(aaid, "parse_listing", lambda html, index_url=aaid.INDEX_URL: _FAKE_ROWS)
    client = _FakeClient()

    assert pipeline.discover(conn, client) == 2

    rows = conn.execute(
        "SELECT case_id, pdf_url, pdf_url_en, lang, status, event_class, "
        "registration, date_of_occurrence FROM aaid_reports ORDER BY case_id"
    ).fetchall()
    assert len(rows) == 2
    assert all(r["status"] == db.STATUS_NEW for r in rows)

    r = next(x for x in rows if x["case_id"] == "5Y-LOL-2024-12-07")
    assert r["lang"] == "en"
    assert r["pdf_url"].endswith("Final%20Report-5Y-LOL.pdf")
    assert r["pdf_url_en"] == r["pdf_url"]
    assert r["event_class"] == "Accident"
    assert r["registration"] == "5Y-LOL"
    assert r["date_of_occurrence"] == "2024-12-07"


def test_discover_idempotent(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(aaid, "parse_listing", lambda html, index_url=aaid.INDEX_URL: _FAKE_ROWS)
    client = _FakeClient()
    assert pipeline.discover(conn, client) == 2
    assert pipeline.discover(conn, client) == 0
    assert conn.execute("SELECT COUNT(*) FROM aaid_reports").fetchone()[0] == 2


def test_discover_skips_existing(monkeypatch):
    conn = _conn()
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO aaid_reports (case_id, status, discovered_at, updated_at) VALUES (?,?,?,?)",
        ("5Y-LOL-2024-12-07", db.STATUS_NEW, ts, ts),
    )
    conn.commit()
    monkeypatch.setattr(aaid, "parse_listing", lambda html, index_url=aaid.INDEX_URL: _FAKE_ROWS)
    assert pipeline.discover(conn, _FakeClient()) == 1


def test_discover_full_flag_accepted(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(aaid, "parse_listing", lambda html, index_url=aaid.INDEX_URL: _FAKE_ROWS)
    assert pipeline.discover(conn, _FakeClient(), full=True) == 2


# ── fetch ─────────────────────────────────────────────────────────────────

def _seed_new(conn, case_id, pdf_url):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO aaid_reports (case_id, pdf_url, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, pdf_url, db.STATUS_NEW, ts, ts),
    )
    conn.commit()


def test_fetch_downloads_and_advances(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "5Y-LOL-2024-12-07", "https://aaid.transport.go.ke/x.pdf")

    calls = []
    def _dl(client, url, dest):
        calls.append((url, dest))
        open(dest, "wb").write(b"%PDF")
    monkeypatch.setattr(aaid, "download", _dl)
    monkeypatch.setattr(aaid, "DELAY", 0)

    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    row = conn.execute("SELECT status, pdf_path FROM aaid_reports").fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert os.path.exists(row["pdf_path"])
    assert len(calls) == 1


def test_fetch_no_url_advances_null_path(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "X-2024-01-01", None)
    calls = []
    monkeypatch.setattr(aaid, "download", lambda *a: calls.append(a))
    monkeypatch.setattr(aaid, "DELAY", 0)
    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    assert not calls
    row = conn.execute("SELECT status, pdf_path FROM aaid_reports").fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert row["pdf_path"] is None


def test_fetch_failure_keeps_new(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "5Y-LOL-2024-12-07", "https://aaid.transport.go.ke/x.pdf")
    monkeypatch.setattr(aaid, "download",
                        lambda c, u, d: (_ for _ in ()).throw(RuntimeError("timeout")))
    monkeypatch.setattr(aaid, "DELAY", 0)
    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    assert conn.execute("SELECT status FROM aaid_reports").fetchone()["status"] == db.STATUS_NEW


def test_fetch_isolates_per_row_errors(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "5Y-LOL-2024-12-07", "https://aaid.transport.go.ke/lol.pdf")
    _seed_new(conn, "ZS-OYI-2010-05-01", "https://aaid.transport.go.ke/oyi.pdf")
    def _dl(c, u, d):
        if "lol" in u:
            raise RuntimeError("403")
        open(d, "wb").write(b"%PDF")
    monkeypatch.setattr(aaid, "download", _dl)
    monkeypatch.setattr(aaid, "DELAY", 0)
    assert pipeline.fetch(conn, None, str(tmp_path)) == 2
    s = {r["case_id"]: r["status"] for r in conn.execute("SELECT case_id, status FROM aaid_reports")}
    assert s["5Y-LOL-2024-12-07"] == db.STATUS_NEW
    assert s["ZS-OYI-2010-05-01"] == db.STATUS_FETCHED


# ── parse ─────────────────────────────────────────────────────────────────

def _seed_fetched(conn, case_id, pdf_path=None):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO aaid_reports (case_id, status, pdf_path, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, db.STATUS_FETCHED, pdf_path, ts, ts),
    )
    conn.commit()


def test_parse_long_narrative(monkeypatch):
    """Good text-layer -> tier 'pdf', OCR fallback NEVER called."""
    conn = _conn()
    _seed_fetched(conn, "5Y-LOL-2024-12-07", pdf_path="x.pdf")
    long_text = "X" * MIN_NARRATIVE
    monkeypatch.setattr(pipeline, "extract_text", lambda p: long_text)
    ocr_calls = []
    monkeypatch.setattr(pipeline, "ocr_extract",
                        lambda p, lang="eng": ocr_calls.append(p) or "")
    assert pipeline.parse(conn) == 1
    row = conn.execute("SELECT narrative_text, source_tier, status FROM aaid_reports").fetchone()
    assert row["status"] == db.STATUS_PARSED
    assert row["source_tier"] == "pdf"
    assert row["narrative_text"] == long_text
    assert ocr_calls == []  # good text-layer is never re-OCR'd


def test_parse_ocr_fallback_recovers_scanned(monkeypatch):
    """text-layer < floor + OCR returns long text -> tier 'ocr', buildable."""
    conn = _conn()
    _seed_fetched(conn, "5Y-LOL-2024-12-07", pdf_path="x.pdf")
    ocr_text = "Y" * 500  # >= _NARRATIVE_FLOOR (80), < MIN_NARRATIVE is irrelevant for ocr tier
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "")
    ocr_calls = []
    def _ocr(p, lang="eng"):
        ocr_calls.append((p, lang))
        return ocr_text
    monkeypatch.setattr(pipeline, "ocr_extract", _ocr)
    assert pipeline.parse(conn) == 1
    row = conn.execute("SELECT source_tier, narrative_text FROM aaid_reports").fetchone()
    assert row["source_tier"] == "ocr"
    assert row["narrative_text"] == ocr_text
    assert ocr_calls == [("x.pdf", "eng")]  # OCR called with lang='eng'
    # 'ocr' tier must be buildable
    conn.execute("UPDATE aaid_reports SET status=? WHERE case_id=?",
                 (db.STATUS_PARSED, "5Y-LOL-2024-12-07"))
    conn.commit()
    assert pipeline.build(conn) == 1


def test_parse_ocr_fallback_blank_scan_stays_none(monkeypatch):
    """text-layer < floor + OCR returns '' -> tier 'none', skipped."""
    conn = _conn()
    _seed_fetched(conn, "5Y-LOL-2024-12-07", pdf_path="x.pdf")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "")
    monkeypatch.setattr(pipeline, "ocr_extract", lambda p, lang="eng": "")
    assert pipeline.parse(conn) == 1
    row = conn.execute("SELECT source_tier, narrative_text FROM aaid_reports").fetchone()
    assert row["source_tier"] == "none"
    assert row["narrative_text"] == ""


def test_parse_short_text_layer_ocr_also_short(monkeypatch):
    """Short non-empty text-layer + OCR also < floor -> falls back to 'short'."""
    conn = _conn()
    _seed_fetched(conn, "5Y-LOL-2024-12-07", pdf_path="x.pdf")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "scanned scraps")
    monkeypatch.setattr(pipeline, "ocr_extract", lambda p, lang="eng": "tiny")
    assert pipeline.parse(conn) == 1
    row = conn.execute("SELECT source_tier, narrative_text FROM aaid_reports").fetchone()
    assert row["source_tier"] == "short"
    assert row["narrative_text"] == "scanned scraps"


def test_parse_no_pdf_path(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "X-2024-01-01", pdf_path=None)
    calls = []
    monkeypatch.setattr(pipeline, "extract_text", lambda p: calls.append(p) or "X" * 1000)
    ocr_calls = []
    monkeypatch.setattr(pipeline, "ocr_extract",
                        lambda p, lang="eng": ocr_calls.append(p) or "")
    assert pipeline.parse(conn) == 1
    assert not calls
    assert not ocr_calls  # no pdf_path -> neither extractor called
    row = conn.execute("SELECT source_tier, narrative_text, status FROM aaid_reports").fetchone()
    assert row["source_tier"] == "none"
    assert row["narrative_text"] == ""
    assert row["status"] == db.STATUS_PARSED


def test_parse_empty_extraction(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "5Y-LOL-2024-12-07", pdf_path="x.pdf")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "")
    monkeypatch.setattr(pipeline, "ocr_extract", lambda p, lang="eng": "")
    pipeline.parse(conn)
    assert conn.execute("SELECT source_tier FROM aaid_reports").fetchone()["source_tier"] == "none"


# ── build ─────────────────────────────────────────────────────────────────

def _seed_parsed(conn, case_id, *, registration=None, location=None, date=None,
                 narrative="", event_class=None, pdf_url=None, report_url=None,
                 aircraft=None, operator=None):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO aaid_reports "
        "(case_id, aircraft, registration, location, date_of_occurrence, "
        "narrative_text, event_class, operator, pdf_url, report_url, "
        "status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (case_id, aircraft, registration, location, date, narrative,
         event_class, operator, pdf_url, report_url, db.STATUS_PARSED, ts, ts),
    )
    conn.commit()


def test_build_creates_accident_row():
    conn = _conn()
    narr = "N" * 200
    _seed_parsed(conn, "5Y-LOL-2024-12-07", registration="5Y-LOL", date="2024-12-07",
                 narrative=narr, event_class="Accident",
                 pdf_url="https://aaid.transport.go.ke/x.pdf")
    assert pipeline.build(conn) == 1
    acc = conn.execute("SELECT * FROM aaid_accidents WHERE case_id='5Y-LOL-2024-12-07'").fetchone()
    assert acc["country"] == "KE"
    assert acc["event_date"] == "2024-12-07"
    assert acc["registration"] == "5Y-LOL"
    assert acc["report_type"] == "Accident"
    assert acc["narrative_text"] == narr
    assert acc["source_url"] == "https://aaid.transport.go.ke/x.pdf"
    assert acc["site_slug"].startswith("crash-")
    assert conn.execute("SELECT status FROM aaid_reports").fetchone()["status"] == db.STATUS_BUILT


def test_build_source_url_falls_back_to_report_url():
    conn = _conn()
    _seed_parsed(conn, "X-2024-01-01", narrative="N" * 200, event_class="Accident",
                 pdf_url=None, report_url=aaid.INDEX_URL)
    pipeline.build(conn)
    acc = conn.execute("SELECT source_url FROM aaid_accidents").fetchone()
    assert acc["source_url"] == aaid.INDEX_URL


def test_build_skips_empty_narrative():
    conn = _conn()
    _seed_parsed(conn, "5Y-A-2024-01-01", narrative="", event_class="Accident",
                 pdf_url="https://aaid.transport.go.ke/x.pdf")
    assert pipeline.build(conn) == 0
    assert conn.execute("SELECT status FROM aaid_reports").fetchone()["status"] == db.STATUS_SKIPPED
    assert conn.execute("SELECT COUNT(*) FROM aaid_accidents").fetchone()[0] == 0


def test_build_skips_below_floor_scanned():
    """79-char narrative (e.g. scanned) -> skipped."""
    conn = _conn()
    _seed_parsed(conn, "5Y-B-2024-01-01", narrative="X" * 79, event_class="Accident")
    assert pipeline.build(conn) == 0
    assert conn.execute("SELECT status FROM aaid_reports").fetchone()["status"] == db.STATUS_SKIPPED


def test_build_country_is_ke():
    conn = _conn()
    _seed_parsed(conn, "5Y-C-2024-01-01", narrative="N" * 200, event_class="Accident")
    pipeline.build(conn)
    assert conn.execute("SELECT country FROM aaid_accidents").fetchone()["country"] == "KE"


def test_build_mixed_rows():
    conn = _conn()
    long_narr = "Z" * 200
    _seed_parsed(conn, "5Y-1-2024-01-01", narrative=long_narr, event_class="Accident")
    _seed_parsed(conn, "5Y-2-2024-01-02", narrative=long_narr, event_class="Accident")
    _seed_parsed(conn, "5Y-3-2024-01-03", narrative="", event_class="Accident")
    assert pipeline.build(conn) == 2
    assert conn.execute("SELECT COUNT(*) FROM aaid_accidents").fetchone()[0] == 2
    assert conn.execute(
        "SELECT status FROM aaid_reports WHERE case_id='5Y-3-2024-01-03'"
    ).fetchone()["status"] == db.STATUS_SKIPPED
