# tests/test_pipeline.py
"""Pipeline tests for bdca discover -> fetch -> parse -> build."""
import os

from bdca_ingest import bdca, db, pipeline
from bdca_ingest.pdf import MIN_NARRATIVE, SCANNED_FLOOR


def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


_FAKE_ROWS = [
    {
        "case_id": "BDCA-2023-V3-HIN",
        "report_url": "https://www.civilaviation.gov.bz/x?download=400:final-report-v3-hin",
        "pdf_url": "https://www.civilaviation.gov.bz/x?download=400:final-report-v3-hin",
        "title": "2023 - Belize Aircraft Accident & Investigation (Final Report V3-HIN)",
        "event_class": "Accident",
        "aircraft": None,
        "registration": "V3-HIN",
        "date_of_occurrence": None,
        "location": None,
        "year": 2023,
    },
    {
        "case_id": "BDCA-2019-N936AN",
        "report_url": "https://www.civilaviation.gov.bz/x?download=256:n936an",
        "pdf_url": "https://www.civilaviation.gov.bz/x?download=256:n936an",
        "title": "2019 - Belize Aircraft Accident & Investigation (Final Report N936AN)",
        "event_class": "Accident",
        "aircraft": None,
        "registration": "N936AN",
        "date_of_occurrence": None,
        "location": None,
        "year": 2019,
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


# ── discover ───────────────────────────────────────────────────────────────

def test_discover_inserts_new_rows(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(bdca, "parse_listing", lambda html: _FAKE_ROWS)
    client = _FakeClient()

    assert pipeline.discover(conn, client) == 2

    rows = conn.execute(
        "SELECT case_id, pdf_url, lang, status, event_class, registration "
        "FROM bdca_reports ORDER BY case_id"
    ).fetchall()
    assert len(rows) == 2
    assert all(r["status"] == db.STATUS_NEW for r in rows)
    assert all(r["lang"] == "en" for r in rows)

    r = next(x for x in rows if x["case_id"] == "BDCA-2023-V3-HIN")
    assert r["pdf_url"].endswith("400:final-report-v3-hin")
    assert r["event_class"] == "Accident"
    assert r["registration"] == "V3-HIN"


def test_discover_idempotent(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(bdca, "parse_listing", lambda html: _FAKE_ROWS)
    client = _FakeClient()
    assert pipeline.discover(conn, client) == 2
    assert pipeline.discover(conn, client) == 0
    assert conn.execute("SELECT COUNT(*) FROM bdca_reports").fetchone()[0] == 2


def test_discover_skips_existing(monkeypatch):
    conn = _conn()
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO bdca_reports (case_id, status, discovered_at, updated_at) VALUES (?,?,?,?)",
        ("BDCA-2023-V3-HIN", db.STATUS_NEW, ts, ts),
    )
    conn.commit()
    monkeypatch.setattr(bdca, "parse_listing", lambda html: _FAKE_ROWS)
    assert pipeline.discover(conn, _FakeClient()) == 1


# ── fetch ──────────────────────────────────────────────────────────────────

def _seed_new(conn, case_id, pdf_url):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO bdca_reports (case_id, pdf_url, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, pdf_url, db.STATUS_NEW, ts, ts),
    )
    conn.commit()


def test_fetch_downloads_and_advances(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "BDCA-2023-V3-HIN", "https://civilaviation.gov.bz/x?download=400:v3-hin")

    calls = []
    def _dl(client, url, dest):
        calls.append((url, dest))
        open(dest, "wb").write(b"%PDF")
    monkeypatch.setattr(bdca, "download", _dl)
    monkeypatch.setattr(bdca, "DELAY", 0)

    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    row = conn.execute(
        "SELECT status, pdf_path FROM bdca_reports WHERE case_id='BDCA-2023-V3-HIN'"
    ).fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert os.path.exists(row["pdf_path"])
    assert len(calls) == 1


def test_fetch_download_failure_keeps_new(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "BDCA-2023-V3-HIN", "https://civilaviation.gov.bz/x?download=400:v3-hin")
    monkeypatch.setattr(
        bdca, "download",
        lambda c, u, d: (_ for _ in ()).throw(RuntimeError("timeout")),
    )
    monkeypatch.setattr(bdca, "DELAY", 0)
    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    assert conn.execute(
        "SELECT status FROM bdca_reports WHERE case_id='BDCA-2023-V3-HIN'"
    ).fetchone()["status"] == db.STATUS_NEW


def test_fetch_isolates_per_row_errors(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "BDCA-2023-V3-HIN", "https://x/?download=400:hin")
    _seed_new(conn, "BDCA-2019-N936AN", "https://x/?download=256:an")

    def _dl(client, url, dest):
        if "400" in url:
            raise RuntimeError("403")
        open(dest, "wb").write(b"%PDF")
    monkeypatch.setattr(bdca, "download", _dl)
    monkeypatch.setattr(bdca, "DELAY", 0)

    assert pipeline.fetch(conn, None, str(tmp_path)) == 2
    assert conn.execute(
        "SELECT status FROM bdca_reports WHERE case_id='BDCA-2023-V3-HIN'"
    ).fetchone()["status"] == db.STATUS_NEW
    assert conn.execute(
        "SELECT status FROM bdca_reports WHERE case_id='BDCA-2019-N936AN'"
    ).fetchone()["status"] == db.STATUS_FETCHED


# ── parse ──────────────────────────────────────────────────────────────────

def _seed_fetched(conn, case_id, pdf_path=None):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO bdca_reports (case_id, status, pdf_path, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, db.STATUS_FETCHED, pdf_path, ts, ts),
    )
    conn.commit()


def test_parse_long_narrative_tier_pdf(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "BDCA-2019-N936AN", pdf_path="x.pdf")
    long_text = "X" * MIN_NARRATIVE
    monkeypatch.setattr(pipeline, "extract_text", lambda p: long_text)
    assert pipeline.parse(conn) == 1
    row = conn.execute(
        "SELECT narrative_text, source_tier, status FROM bdca_reports WHERE case_id='BDCA-2019-N936AN'"
    ).fetchone()
    assert row["status"] == db.STATUS_PARSED
    assert row["source_tier"] == "pdf"
    assert row["narrative_text"] == long_text


def test_parse_short_tier(monkeypatch):
    """Text between SCANNED_FLOOR and MIN_NARRATIVE -> 'short'."""
    conn = _conn()
    _seed_fetched(conn, "BDCA-2023-V3-HIN", pdf_path="x.pdf")
    txt = "Y" * (SCANNED_FLOOR + 10)
    monkeypatch.setattr(pipeline, "extract_text", lambda p: txt)
    assert pipeline.parse(conn) == 1
    assert conn.execute(
        "SELECT source_tier FROM bdca_reports WHERE case_id='BDCA-2023-V3-HIN'"
    ).fetchone()["source_tier"] == "short"


def test_parse_scanned_tier(monkeypatch):
    """Image-only PDF (tiny text layer) -> 'scanned'."""
    conn = _conn()
    _seed_fetched(conn, "BDCA-1991-N402BL", pdf_path="x.pdf")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "\f \f")  # ~3 chars
    assert pipeline.parse(conn) == 1
    assert conn.execute(
        "SELECT source_tier FROM bdca_reports WHERE case_id='BDCA-1991-N402BL'"
    ).fetchone()["source_tier"] == "scanned"


def test_parse_no_pdf_path_tier_none(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "BDCA-2023-V3-HIN", pdf_path=None)
    calls = []
    monkeypatch.setattr(pipeline, "extract_text", lambda p: calls.append(p) or "X" * 1000)
    assert pipeline.parse(conn) == 1
    assert not calls
    assert conn.execute(
        "SELECT source_tier FROM bdca_reports WHERE case_id='BDCA-2023-V3-HIN'"
    ).fetchone()["source_tier"] == "none"


# ── build ──────────────────────────────────────────────────────────────────

def _seed_parsed(conn, case_id, *, aircraft=None, registration=None,
                 location=None, date=None, narrative="", event_class=None,
                 operator=None, pdf_url=None, report_url=None):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO bdca_reports "
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
    narr = "N" * 700
    _seed_parsed(
        conn, "BDCA-2023-V3-HIN",
        registration="V3-HIN", narrative=narr, event_class="Accident",
        pdf_url="https://civilaviation.gov.bz/x?download=400:v3-hin",
    )
    assert pipeline.build(conn) == 1
    acc = conn.execute(
        "SELECT * FROM bdca_accidents WHERE case_id='BDCA-2023-V3-HIN'"
    ).fetchone()
    assert acc["country"] == "BZ"
    assert acc["registration"] == "V3-HIN"
    assert acc["report_type"] == "Accident"
    assert acc["narrative_text"] == narr
    assert acc["probable_cause"] is None
    assert acc["source_url"].endswith("400:v3-hin")
    assert acc["site_slug"].startswith("crash-")
    assert conn.execute(
        "SELECT status FROM bdca_reports WHERE case_id='BDCA-2023-V3-HIN'"
    ).fetchone()["status"] == db.STATUS_BUILT


def test_build_source_url_falls_back_to_report_url():
    conn = _conn()
    _seed_parsed(
        conn, "BDCA-2019-N936AN", narrative="N" * (pipeline._NARRATIVE_FLOOR + 100), event_class="Accident",
        pdf_url=None, report_url="https://civilaviation.gov.bz/report",
    )
    pipeline.build(conn)
    assert conn.execute(
        "SELECT source_url FROM bdca_accidents WHERE case_id='BDCA-2019-N936AN'"
    ).fetchone()["source_url"] == "https://civilaviation.gov.bz/report"


def test_build_skips_scanned_short_narrative():
    """A scanned report (tiny narrative) is skipped, not built."""
    conn = _conn()
    _seed_parsed(conn, "BDCA-1991-N402BL", narrative="\f", event_class="Accident")
    assert pipeline.build(conn) == 0
    assert conn.execute(
        "SELECT status FROM bdca_reports WHERE case_id='BDCA-1991-N402BL'"
    ).fetchone()["status"] == db.STATUS_SKIPPED
    assert conn.execute("SELECT COUNT(*) FROM bdca_accidents").fetchone()[0] == 0


def test_build_country_is_bz():
    conn = _conn()
    _seed_parsed(conn, "BDCA-2023-V3-HIN", narrative="N" * (pipeline._NARRATIVE_FLOOR + 100), event_class="Accident")
    pipeline.build(conn)
    assert conn.execute(
        "SELECT country FROM bdca_accidents WHERE case_id='BDCA-2023-V3-HIN'"
    ).fetchone()["country"] == "BZ"


def test_build_mixed_rows():
    conn = _conn()
    narr = "Z" * (pipeline._NARRATIVE_FLOOR + 100)
    _seed_parsed(conn, "BDCA-2023-V3-HIN", narrative=narr, event_class="Accident")
    _seed_parsed(conn, "BDCA-2019-N936AN", narrative=narr, event_class="Accident")
    _seed_parsed(conn, "BDCA-1991-N402BL", narrative="", event_class="Accident")
    assert pipeline.build(conn) == 2
    assert conn.execute("SELECT COUNT(*) FROM bdca_accidents").fetchone()[0] == 2
    assert conn.execute(
        "SELECT status FROM bdca_reports WHERE case_id='BDCA-1991-N402BL'"
    ).fetchone()["status"] == db.STATUS_SKIPPED
