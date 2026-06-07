# tests/test_pipeline.py
"""Pipeline tests for ciaape discover → fetch → parse → build."""
import os

from ciaape_ingest import ciaape, db, pipeline
from ciaape_ingest.pdf import MIN_NARRATIVE
from ciaape_ingest.pipeline import SCANNED_MIN


def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


_FAKE_ROWS = [
    {
        "case_id": "CIAA-ACCID-008-2022",
        "report_url": "https://www.gob.pe/institucion/mtc/informes-publicaciones/1-informe-final-ciaa-accid-008-2022",
        "event_class": "Accident",
        "registration": "CC-BHB",
        "date_of_occurrence": "2022-11-18",
        "report_type": "Informe Final",
        "title": "Informe Final CIAA-ACCID-008-2022, Matricula CC-BHB, Fecha 18/11/2022",
    },
    {
        "case_id": "CIAA-SINCID-001-2022",
        "report_url": "https://www.gob.pe/institucion/mtc/informes-publicaciones/2-informe-final-ciaa-sincid-001-2022",
        "event_class": "Serious incident",
        "registration": "OB-2214",
        "date_of_occurrence": "2022-01-28",
        "report_type": "Informe Final",
        "title": "Informe Final CIAA-SINCID-001-2022, Matricula OB-2214, Fecha 28/01/2022",
    },
]


class _FakeResp:
    def __init__(self, body="", status=200):
        self.content = body.encode("utf-8") if isinstance(body, str) else body
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _SheetClient:
    """Returns _FAKE_ROWS-worth of HTML for sheet 1, empty for later sheets."""
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if "sheet=1" in url:
            return _FakeResp("<rows/>")
        return _FakeResp("<empty/>")


# ── discover ────────────────────────────────────────────────────────────────────

def test_discover_inserts_new_rows(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(ciaape, "parse_collection",
                        lambda html: _FAKE_ROWS if "<rows/>" in html else [])
    monkeypatch.setattr(ciaape, "DELAY", 0)
    client = _SheetClient()

    assert pipeline.discover(conn, client) == 2
    rows = conn.execute(
        "SELECT case_id, report_url, event_class, registration, date_of_occurrence, "
        "lang, status FROM ciaape_reports ORDER BY case_id"
    ).fetchall()
    assert len(rows) == 2
    assert all(r["status"] == db.STATUS_NEW for r in rows)
    assert all(r["lang"] == "es" for r in rows)
    r = next(x for x in rows if x["case_id"] == "CIAA-ACCID-008-2022")
    assert r["event_class"] == "Accident"
    assert r["registration"] == "CC-BHB"
    assert r["date_of_occurrence"] == "2022-11-18"
    assert r["report_url"].startswith("https://www.gob.pe/")


def test_discover_idempotent(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(ciaape, "parse_collection",
                        lambda html: _FAKE_ROWS if "<rows/>" in html else [])
    monkeypatch.setattr(ciaape, "DELAY", 0)
    client = _SheetClient()
    assert pipeline.discover(conn, client) == 2
    assert pipeline.discover(conn, client) == 0
    assert conn.execute("SELECT COUNT(*) FROM ciaape_reports").fetchone()[0] == 2


def test_discover_stops_at_first_empty_sheet(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(ciaape, "parse_collection",
                        lambda html: _FAKE_ROWS if "<rows/>" in html else [])
    monkeypatch.setattr(ciaape, "DELAY", 0)
    client = _SheetClient()
    pipeline.discover(conn, client)
    # sheet1 (rows) + sheet2 (empty → break); must NOT walk to MAX_SHEETS
    assert client.calls == [ciaape.sheet_url(1), ciaape.sheet_url(2)]


def test_discover_full_flag_accepted(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(ciaape, "parse_collection",
                        lambda html: _FAKE_ROWS if "<rows/>" in html else [])
    monkeypatch.setattr(ciaape, "DELAY", 0)
    assert pipeline.discover(conn, _SheetClient(), full=True) == 2


def _prov_then_final(provisional_first):
    """Build a parse_collection stub serving one case_id: a provisional row and
    an Informe Final row for the same case, ordered per `provisional_first`."""
    prov = {
        "case_id": "CIAA-ACCID-009-2022",
        "report_url": "https://www.gob.pe/x/3-declaracion-provisional-ciaa-accid-009-2022",
        "event_class": "Accident", "registration": "OB-9000",
        "date_of_occurrence": "2022-05-01", "report_type": "Declaracion Provisional",
        "title": "Declaracion Provisional CIAA-ACCID-009-2022",
    }
    final = {
        "case_id": "CIAA-ACCID-009-2022",
        "report_url": "https://www.gob.pe/x/4-informe-final-ciaa-accid-009-2022",
        "event_class": "Accident", "registration": "OB-9000",
        "date_of_occurrence": "2022-05-01", "report_type": "Informe Final",
        "title": "Informe Final CIAA-ACCID-009-2022",
    }
    return [prov, final] if provisional_first else [final, prov]


def test_discover_upgrades_provisional_to_final(monkeypatch):
    """A stored provisional row is upgraded in place when an Informe Final is
    later discovered (cross-run), and its status is reset to 'new'."""
    conn = _conn()
    monkeypatch.setattr(ciaape, "DELAY", 0)
    # Run 1: only the provisional exists on the site.
    monkeypatch.setattr(ciaape, "parse_collection",
                        lambda html: _prov_then_final(True)[:1] if "<rows/>" in html else [])
    pipeline.discover(conn, _SheetClient())
    row = conn.execute("SELECT report_url, status FROM ciaape_reports").fetchone()
    assert "declaracion-provisional" in row["report_url"]
    # Simulate that the provisional was already processed.
    conn.execute("UPDATE ciaape_reports SET status=?", (db.STATUS_FETCHED,))
    conn.commit()
    # Run 2: the Informe Final is now published.
    monkeypatch.setattr(ciaape, "parse_collection",
                        lambda html: _prov_then_final(True)[1:] if "<rows/>" in html else [])
    pipeline.discover(conn, _SheetClient())
    row = conn.execute("SELECT report_url, status FROM ciaape_reports").fetchone()
    assert "informe-final" in row["report_url"]
    assert row["status"] == db.STATUS_NEW
    assert conn.execute("SELECT COUNT(*) FROM ciaape_reports").fetchone()[0] == 1


def test_discover_final_not_downgraded_by_provisional(monkeypatch):
    """A stored Informe Final is never replaced by a later provisional."""
    conn = _conn()
    monkeypatch.setattr(ciaape, "DELAY", 0)
    monkeypatch.setattr(ciaape, "parse_collection",
                        lambda html: _prov_then_final(False)[:1] if "<rows/>" in html else [])
    pipeline.discover(conn, _SheetClient())
    conn.execute("UPDATE ciaape_reports SET status=?", (db.STATUS_FETCHED,))
    conn.commit()
    monkeypatch.setattr(ciaape, "parse_collection",
                        lambda html: _prov_then_final(False)[1:] if "<rows/>" in html else [])
    pipeline.discover(conn, _SheetClient())
    row = conn.execute("SELECT report_url, status FROM ciaape_reports").fetchone()
    assert "informe-final" in row["report_url"]
    assert row["status"] == db.STATUS_FETCHED  # untouched


def test_discover_idempotent_on_final(monkeypatch):
    """Re-discovering the same Informe Final does not reset an existing row."""
    conn = _conn()
    monkeypatch.setattr(ciaape, "DELAY", 0)
    monkeypatch.setattr(ciaape, "parse_collection",
                        lambda html: _prov_then_final(True)[1:] if "<rows/>" in html else [])
    pipeline.discover(conn, _SheetClient())
    conn.execute("UPDATE ciaape_reports SET status=?", (db.STATUS_FETCHED,))
    conn.commit()
    pipeline.discover(conn, _SheetClient())
    row = conn.execute("SELECT status FROM ciaape_reports").fetchone()
    assert row["status"] == db.STATUS_FETCHED  # no spurious reset


# ── fetch (report-page hop → cdn PDF) ────────────────────────────────────────────

def _seed_new(conn, case_id, report_url):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO ciaape_reports (case_id, report_url, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, report_url, db.STATUS_NEW, ts, ts),
    )
    conn.commit()


class _ReportClient:
    """get(report_url) → report HTML containing PDF; download writes bytes."""
    def __init__(self, page_html):
        self._page_html = page_html

    def get(self, url, **kwargs):
        return _FakeResp(self._page_html)


def test_fetch_hops_to_report_page_then_downloads(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "CIAA-ACCID-008-2022",
              "https://www.gob.pe/institucion/mtc/informes-publicaciones/1-x")
    pdf_cdn = "https://cdn.www.gob.pe/uploads/document/file/5209692/x.pdf"
    page = f'<a href="{pdf_cdn}">PDF</a>'

    dl = []
    def _fake_download(client, url, dest):
        dl.append((url, dest))
        open(dest, "wb").write(b"%PDF data")

    monkeypatch.setattr(ciaape, "download", _fake_download)
    monkeypatch.setattr(ciaape, "DELAY", 0)

    assert pipeline.fetch(conn, _ReportClient(page), str(tmp_path)) == 1
    row = conn.execute(
        "SELECT status, pdf_url, pdf_path FROM ciaape_reports WHERE case_id='CIAA-ACCID-008-2022'"
    ).fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert row["pdf_url"] == pdf_cdn
    assert row["pdf_path"] is not None and os.path.exists(row["pdf_path"])
    assert len(dl) == 1


def test_fetch_no_pdf_on_page_advances_with_null_path(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "CIAA-ACCID-009-2022",
              "https://www.gob.pe/institucion/mtc/informes-publicaciones/9-x")
    dl = []
    monkeypatch.setattr(ciaape, "download", lambda *a: dl.append(a))
    monkeypatch.setattr(ciaape, "DELAY", 0)

    assert pipeline.fetch(conn, _ReportClient("<html>no pdf</html>"), str(tmp_path)) == 1
    assert not dl  # download not called
    row = conn.execute(
        "SELECT status, pdf_path FROM ciaape_reports WHERE case_id='CIAA-ACCID-009-2022'"
    ).fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert row["pdf_path"] is None


def test_fetch_download_failure_keeps_row_new(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "CIAA-ACCID-008-2022",
              "https://www.gob.pe/institucion/mtc/informes-publicaciones/1-x")
    page = '<a href="https://cdn.www.gob.pe/uploads/document/file/1/x.pdf">PDF</a>'
    monkeypatch.setattr(
        ciaape, "download",
        lambda c, u, d: (_ for _ in ()).throw(RuntimeError("timeout")),
    )
    monkeypatch.setattr(ciaape, "DELAY", 0)
    assert pipeline.fetch(conn, _ReportClient(page), str(tmp_path)) == 1
    assert conn.execute(
        "SELECT status FROM ciaape_reports WHERE case_id='CIAA-ACCID-008-2022'"
    ).fetchone()["status"] == db.STATUS_NEW


# ── parse (scanned gate) ─────────────────────────────────────────────────────────

def _seed_fetched(conn, case_id, pdf_path=None):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO ciaape_reports (case_id, status, pdf_path, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, db.STATUS_FETCHED, pdf_path, ts, ts),
    )
    conn.commit()


def test_parse_long_narrative(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "CIAA-ACCID-008-2022", pdf_path="x.pdf")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "X" * MIN_NARRATIVE)
    assert pipeline.parse(conn) == 1
    row = conn.execute(
        "SELECT source_tier, status FROM ciaape_reports WHERE case_id='CIAA-ACCID-008-2022'"
    ).fetchone()
    assert row["status"] == db.STATUS_PARSED
    assert row["source_tier"] == "pdf"


def test_parse_thin_but_real_narrative_is_short_tier(monkeypatch):
    """Peru narratives are thin: >= SCANNED_MIN but < MIN_NARRATIVE → 'short', buildable."""
    conn = _conn()
    _seed_fetched(conn, "CIAA-ACCID-008-2022", pdf_path="x.pdf")
    text = "Y" * (SCANNED_MIN + 10)
    monkeypatch.setattr(pipeline, "extract_text", lambda p: text)
    pipeline.parse(conn)
    row = conn.execute(
        "SELECT source_tier, narrative_text FROM ciaape_reports WHERE case_id='CIAA-ACCID-008-2022'"
    ).fetchone()
    assert row["source_tier"] == "short"
    assert row["narrative_text"] == text


def test_parse_scanned_pdf_gate(monkeypatch):
    """< SCANNED_MIN chars from a PDF → 'scanned'."""
    conn = _conn()
    _seed_fetched(conn, "CIAA-ACCID-008-2022", pdf_path="x.pdf")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "tiny")
    pipeline.parse(conn)
    assert conn.execute(
        "SELECT source_tier FROM ciaape_reports WHERE case_id='CIAA-ACCID-008-2022'"
    ).fetchone()["source_tier"] == "scanned"


def test_parse_no_pdf_path(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "CIAA-ACCID-009-2022", pdf_path=None)
    calls = []
    monkeypatch.setattr(pipeline, "extract_text", lambda p: calls.append(p) or "X" * 1000)
    assert pipeline.parse(conn) == 1
    assert not calls
    assert conn.execute(
        "SELECT source_tier FROM ciaape_reports WHERE case_id='CIAA-ACCID-009-2022'"
    ).fetchone()["source_tier"] == "none"


# ── build ─────────────────────────────────────────────────────────────────────

def _seed_parsed(conn, case_id, *, aircraft=None, registration=None, location=None,
                 date=None, narrative="", event_class=None, source_tier="short",
                 pdf_url=None, report_url=None):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO ciaape_reports "
        "(case_id, aircraft, registration, location, date_of_occurrence, narrative_text, "
        "event_class, source_tier, pdf_url, report_url, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (case_id, aircraft, registration, location, date, narrative, event_class,
         source_tier, pdf_url, report_url, db.STATUS_PARSED, ts, ts),
    )
    conn.commit()


def test_build_creates_accident_row_country_pe():
    conn = _conn()
    _seed_parsed(conn, "CIAA-ACCID-008-2022", aircraft="AIRBUS A320N",
                 registration="CC-BHB", location="Lima", date="2022-11-18",
                 narrative="N" * 600, event_class="Accident", source_tier="short",
                 pdf_url="https://cdn.www.gob.pe/uploads/document/file/1/x.pdf")
    assert pipeline.build(conn) == 1
    acc = conn.execute(
        "SELECT * FROM ciaape_accidents WHERE case_id='CIAA-ACCID-008-2022'"
    ).fetchone()
    assert acc["country"] == "PE"
    assert acc["event_date"] == "2022-11-18"
    assert acc["registration"] == "CC-BHB"
    assert acc["report_type"] == "Accident"
    assert acc["source_url"].startswith("https://cdn.www.gob.pe/")
    assert acc["site_slug"].startswith("crash-")
    assert conn.execute(
        "SELECT status FROM ciaape_reports WHERE case_id='CIAA-ACCID-008-2022'"
    ).fetchone()["status"] == db.STATUS_BUILT


def test_build_source_url_falls_back_to_report_url():
    conn = _conn()
    _seed_parsed(conn, "CIAA-ACCID-009-2022", narrative="N" * 200,
                 event_class="Accident", source_tier="short", pdf_url=None,
                 report_url="https://www.gob.pe/institucion/mtc/informes-publicaciones/9-x")
    pipeline.build(conn)
    assert conn.execute(
        "SELECT source_url FROM ciaape_accidents WHERE case_id='CIAA-ACCID-009-2022'"
    ).fetchone()["source_url"].startswith("https://www.gob.pe/")


def test_build_skips_scanned_tier():
    conn = _conn()
    _seed_parsed(conn, "CIAA-ACCID-010-2022", narrative="tiny", event_class="Accident",
                 source_tier="scanned")
    assert pipeline.build(conn) == 0
    assert conn.execute(
        "SELECT status FROM ciaape_reports WHERE case_id='CIAA-ACCID-010-2022'"
    ).fetchone()["status"] == db.STATUS_SKIPPED
    assert conn.execute("SELECT COUNT(*) FROM ciaape_accidents").fetchone()[0] == 0


def test_build_skips_none_tier():
    conn = _conn()
    _seed_parsed(conn, "CIAA-ACCID-011-2022", narrative="", event_class="Accident",
                 source_tier="none")
    assert pipeline.build(conn) == 0
    assert conn.execute(
        "SELECT status FROM ciaape_reports WHERE case_id='CIAA-ACCID-011-2022'"
    ).fetchone()["status"] == db.STATUS_SKIPPED


def test_build_skips_below_floor():
    conn = _conn()
    _seed_parsed(conn, "CIAA-ACCID-012-2022", narrative="X" * 79,
                 event_class="Serious incident", source_tier="short")
    assert pipeline.build(conn) == 0
    assert conn.execute(
        "SELECT status FROM ciaape_reports WHERE case_id='CIAA-ACCID-012-2022'"
    ).fetchone()["status"] == db.STATUS_SKIPPED


def test_build_report_type_from_event_class():
    conn = _conn()
    _seed_parsed(conn, "CIAA-SINCID-001-2022", narrative="N" * 200,
                 event_class="Serious incident", source_tier="pdf")
    pipeline.build(conn)
    assert conn.execute(
        "SELECT report_type FROM ciaape_accidents WHERE case_id='CIAA-SINCID-001-2022'"
    ).fetchone()["report_type"] == "Serious incident"
