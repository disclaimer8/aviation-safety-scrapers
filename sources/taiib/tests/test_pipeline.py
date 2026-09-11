"""Pipeline tests for taiib discover -> fetch -> parse -> build."""
import os

from taiib_ingest import taiib, db, pipeline
from taiib_ingest.pdf import MIN_NARRATIVE
from taiib_ingest.pipeline import SCANNED_FLOOR


def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


_FAKE_ROWS = [
    {
        "case_id": "yl-eva-2024-05-04",
        "report_url": taiib.INDEX_URL,
        "pdf_url": "https://www.taiib.gov.lv/lv/media/665/download?attachment",
        "pdf_url_es": None,
        "pdf_url_en": None,
        "title": "Adazi 4.05.2024_NZ.pdf",
        "event_class": "Accident",
        "aircraft": None,
        "registration": "YL-EVA",
        "date_of_occurrence": "2024-05-04",
        "location": None,
        "lang": "lv",
        "reference": None,
    },
    {
        "case_id": "yl-aap-2023-03-08",
        "report_url": taiib.INDEX_URL,
        "pdf_url": "https://www.taiib.gov.lv/lv/media/603/download?attachment",
        "pdf_url_es": None,
        "pdf_url_en": None,
        "title": "rix-8.03.2023.pdf",
        "event_class": "Serious incident",
        "aircraft": None,
        "registration": "YL-AAP",
        "date_of_occurrence": "2023-03-08",
        "location": None,
        "lang": "en",
        "reference": None,
    },
    {
        "case_id": "m67",
        "report_url": taiib.INDEX_URL,
        "pdf_url": "https://www.taiib.gov.lv/lv/media/67/download?attachment",
        "pdf_url_es": None,
        "pdf_url_en": None,
        "title": "madona-16.10.18_nz.pdf",
        "event_class": "Accident",
        "aircraft": None,
        "registration": None,
        "date_of_occurrence": None,
        "location": None,
        "lang": "lv",
        "reference": None,
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


# ── discover ──────────────────────────────────────────────────────────────────

def test_discover_inserts_new_rows(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(taiib, "parse_listing", lambda html, year_url="": _FAKE_ROWS)
    client = _FakeClient()

    assert pipeline.discover(conn, client) == 3
    rows = conn.execute(
        "SELECT case_id, pdf_url, registration, date_of_occurrence, event_class, "
        "lang, status FROM taiib_reports ORDER BY case_id"
    ).fetchall()
    assert len(rows) == 3
    assert all(r["status"] == db.STATUS_NEW for r in rows)

    eva = next(r for r in rows if r["case_id"] == "yl-eva-2024-05-04")
    assert eva["pdf_url"].endswith("/lv/media/665/download?attachment")
    assert eva["registration"] == "YL-EVA"
    assert eva["date_of_occurrence"] == "2024-05-04"
    assert eva["event_class"] == "Accident"
    assert eva["lang"] == "lv"

    # discover hits ONLY the single index URL — no per-year walk
    assert client.calls == [taiib.INDEX_URL]


def test_discover_idempotent(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(taiib, "parse_listing", lambda html, year_url="": _FAKE_ROWS)
    client = _FakeClient()
    assert pipeline.discover(conn, client) == 3
    assert pipeline.discover(conn, client) == 0
    assert conn.execute("SELECT COUNT(*) FROM taiib_reports").fetchone()[0] == 3


def test_discover_skips_existing(monkeypatch):
    conn = _conn()
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO taiib_reports (case_id, status, discovered_at, updated_at) VALUES (?,?,?,?)",
        ("yl-eva-2024-05-04", db.STATUS_NEW, ts, ts),
    )
    conn.commit()
    monkeypatch.setattr(taiib, "parse_listing", lambda html, year_url="": _FAKE_ROWS)
    assert pipeline.discover(conn, _FakeClient()) == 2


def test_discover_full_flag_accepted(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(taiib, "parse_listing", lambda html, year_url="": _FAKE_ROWS)
    assert pipeline.discover(conn, _FakeClient(), full=True) == 3


# ── fetch ─────────────────────────────────────────────────────────────────────

def _seed_new(conn, case_id, pdf_url):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO taiib_reports (case_id, pdf_url, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, pdf_url, db.STATUS_NEW, ts, ts),
    )
    conn.commit()


def test_fetch_downloads_and_advances(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "yl-eva-2024-05-04", "https://www.taiib.gov.lv/lv/media/665/download?attachment")
    calls = []
    def _dl(client, url, dest):
        calls.append((url, dest)); open(dest, "wb").write(b"%PDF")
    monkeypatch.setattr(taiib, "download", _dl)
    monkeypatch.setattr(taiib, "DELAY", 0)

    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    row = conn.execute(
        "SELECT status, pdf_path FROM taiib_reports WHERE case_id='yl-eva-2024-05-04'"
    ).fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert os.path.exists(row["pdf_path"])
    assert len(calls) == 1


def test_fetch_no_url_advances_null(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "m99", None)
    calls = []
    monkeypatch.setattr(taiib, "download", lambda *a: calls.append(a))
    monkeypatch.setattr(taiib, "DELAY", 0)
    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    assert not calls
    row = conn.execute("SELECT status, pdf_path FROM taiib_reports WHERE case_id='m99'").fetchone()
    assert row["status"] == db.STATUS_FETCHED and row["pdf_path"] is None


def test_fetch_failure_keeps_new(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "yl-eva-2024-05-04", "https://www.taiib.gov.lv/lv/media/665/download?attachment")
    monkeypatch.setattr(taiib, "download",
                        lambda c, u, d: (_ for _ in ()).throw(RuntimeError("timeout")))
    monkeypatch.setattr(taiib, "DELAY", 0)
    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    assert conn.execute(
        "SELECT status FROM taiib_reports WHERE case_id='yl-eva-2024-05-04'"
    ).fetchone()["status"] == db.STATUS_NEW


def test_fetch_isolates_per_row(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "yl-eva-2024-05-04", "https://www.taiib.gov.lv/lv/media/665/download?attachment")
    _seed_new(conn, "yl-aap-2023-03-08", "https://www.taiib.gov.lv/lv/media/603/download?attachment")
    def _sel(client, url, dest):
        if "665" in url: raise RuntimeError("403")
        open(dest, "wb").write(b"%PDF")
    monkeypatch.setattr(taiib, "download", _sel)
    monkeypatch.setattr(taiib, "DELAY", 0)
    assert pipeline.fetch(conn, None, str(tmp_path)) == 2
    assert conn.execute("SELECT status FROM taiib_reports WHERE case_id='yl-eva-2024-05-04'").fetchone()["status"] == db.STATUS_NEW
    assert conn.execute("SELECT status FROM taiib_reports WHERE case_id='yl-aap-2023-03-08'").fetchone()["status"] == db.STATUS_FETCHED


# ── parse ─────────────────────────────────────────────────────────────────────

def _seed_fetched(conn, case_id, pdf_path=None):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO taiib_reports (case_id, status, pdf_path, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, db.STATUS_FETCHED, pdf_path, ts, ts),
    )
    conn.commit()


def test_parse_long_pdf_tier(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "yl-eva-2024-05-04", pdf_path="x.pdf")
    txt = "X" * MIN_NARRATIVE
    monkeypatch.setattr(pipeline, "extract_text", lambda p: txt)
    assert pipeline.parse(conn) == 1
    row = conn.execute("SELECT source_tier, narrative_text, status FROM taiib_reports WHERE case_id='yl-eva-2024-05-04'").fetchone()
    assert row["source_tier"] == "pdf"
    assert row["narrative_text"] == txt
    assert row["status"] == db.STATUS_PARSED


def test_parse_short_tier(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "x", pdf_path="x.pdf")
    txt = "Y" * (SCANNED_FLOOR + 10)
    monkeypatch.setattr(pipeline, "extract_text", lambda p: txt)
    pipeline.parse(conn)
    assert conn.execute("SELECT source_tier FROM taiib_reports WHERE case_id='x'").fetchone()["source_tier"] == "short"


def test_parse_scanned_tier(monkeypatch):
    """Text present but below SCANNED_FLOOR → tier='scanned'."""
    conn = _conn()
    _seed_fetched(conn, "x", pdf_path="x.pdf")
    txt = "Z" * 100
    monkeypatch.setattr(pipeline, "extract_text", lambda p: txt)
    pipeline.parse(conn)
    assert conn.execute("SELECT source_tier FROM taiib_reports WHERE case_id='x'").fetchone()["source_tier"] == "scanned"


def test_parse_no_pdf(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "m99", pdf_path=None)
    calls = []
    monkeypatch.setattr(pipeline, "extract_text", lambda p: calls.append(p) or "X" * 1000)
    assert pipeline.parse(conn) == 1
    assert not calls
    row = conn.execute("SELECT source_tier, narrative_text FROM taiib_reports WHERE case_id='m99'").fetchone()
    assert row["source_tier"] == "none" and row["narrative_text"] == ""


def test_parse_empty_extraction(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "x", pdf_path="x.pdf")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "")
    pipeline.parse(conn)
    assert conn.execute("SELECT source_tier FROM taiib_reports WHERE case_id='x'").fetchone()["source_tier"] == "none"


# ── build ─────────────────────────────────────────────────────────────────────

def _seed_parsed(conn, case_id, *, aircraft=None, registration=None, location=None,
                 date=None, narrative="", event_class=None, source_tier="pdf",
                 pdf_url=None, report_url=None):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO taiib_reports "
        "(case_id, aircraft, registration, location, date_of_occurrence, narrative_text, "
        "event_class, source_tier, pdf_url, report_url, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (case_id, aircraft, registration, location, date, narrative, event_class,
         source_tier, pdf_url, report_url, db.STATUS_PARSED, ts, ts),
    )
    conn.commit()


def test_build_creates_row():
    conn = _conn()
    narr = "N" * 700
    _seed_parsed(conn, "yl-eva-2024-05-04", aircraft="Tecnam P2008", registration="YL-EVA",
                 location="Adazi", date="2024-05-04", narrative=narr, event_class="Accident",
                 pdf_url="https://www.taiib.gov.lv/lv/media/665/download?attachment")
    assert pipeline.build(conn) == 1
    acc = conn.execute("SELECT * FROM taiib_accidents WHERE case_id='yl-eva-2024-05-04'").fetchone()
    assert acc["country"] == "LV"
    assert acc["event_date"] == "2024-05-04"
    assert acc["registration"] == "YL-EVA"
    assert acc["report_type"] == "Accident"
    assert acc["narrative_text"] == narr
    assert acc["source_url"].endswith("/lv/media/665/download?attachment")
    assert acc["site_slug"].startswith("crash-")
    assert conn.execute("SELECT status FROM taiib_reports WHERE case_id='yl-eva-2024-05-04'").fetchone()["status"] == db.STATUS_BUILT


def test_build_source_url_falls_back_to_report_url():
    conn = _conn()
    _seed_parsed(conn, "m67", narrative="N" * (pipeline._NARRATIVE_FLOOR + 100), event_class="Accident",
                 pdf_url=None, report_url=taiib.INDEX_URL)
    pipeline.build(conn)
    assert conn.execute("SELECT source_url FROM taiib_accidents WHERE case_id='m67'").fetchone()["source_url"] == taiib.INDEX_URL


def test_build_skips_empty_narrative():
    conn = _conn()
    _seed_parsed(conn, "x", narrative="", event_class="Accident")
    assert pipeline.build(conn) == 0
    assert conn.execute("SELECT status FROM taiib_reports WHERE case_id='x'").fetchone()["status"] == db.STATUS_SKIPPED
    assert conn.execute("SELECT COUNT(*) FROM taiib_accidents").fetchone()[0] == 0


def test_build_skips_below_floor():
    conn = _conn()
    _seed_parsed(conn, "x", narrative="X" * (pipeline._NARRATIVE_FLOOR - 1), event_class="Accident")
    assert pipeline.build(conn) == 0
    assert conn.execute("SELECT status FROM taiib_reports WHERE case_id='x'").fetchone()["status"] == db.STATUS_SKIPPED


def test_build_skips_scanned_tier():
    """Scanned (image-only) PDFs are skipped even with long-ish text."""
    conn = _conn()
    _seed_parsed(conn, "x", narrative="X" * (pipeline._NARRATIVE_FLOOR + 100), event_class="Accident", source_tier="scanned")
    assert pipeline.build(conn) == 0
    assert conn.execute("SELECT status FROM taiib_reports WHERE case_id='x'").fetchone()["status"] == db.STATUS_SKIPPED


def test_build_country_is_lv():
    conn = _conn()
    _seed_parsed(conn, "x", narrative="N" * (pipeline._NARRATIVE_FLOOR + 100), event_class="Accident")
    pipeline.build(conn)
    assert conn.execute("SELECT country FROM taiib_accidents WHERE case_id='x'").fetchone()["country"] == "LV"


def test_build_null_metadata_full_narrative_built():
    conn = _conn()
    _seed_parsed(conn, "m68", narrative="N" * 700, event_class=None)
    assert pipeline.build(conn) == 1
    acc = conn.execute("SELECT * FROM taiib_accidents WHERE case_id='m68'").fetchone()
    assert acc["aircraft"] is None and acc["registration"] is None
    assert acc["site_slug"] == "crash-taiib"
