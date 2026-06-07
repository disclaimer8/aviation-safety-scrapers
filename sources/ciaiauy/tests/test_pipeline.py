"""Pipeline tests for ciaiauy discover -> fetch -> parse -> build."""
import os

from ciaiauy_ingest import ciaiauy, db, pipeline
from ciaiauy_ingest.pdf import MIN_NARRATIVE, SCANNED_MAX


def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


_FAKE_ROWS = [
    {"pdf_url": "https://www.gub.uy/x/611-cx-ota-r.pdf", "title": "611 CX-OTA-R",
     "registration": "CX-OTA-R", "event_class": "Serious incident",
     "caso": "611", "date_of_occurrence": None},
    {"pdf_url": "https://www.gub.uy/x/informe-final-cx-mgp.pdf", "title": "Informe Final CX-MGP",
     "registration": "CX-MGP", "event_class": "Accident",
     "caso": None, "date_of_occurrence": None},
    {"pdf_url": "https://www.gub.uy/x/informe-final-lv-wiz.pdf", "title": "Informe Final LV-WIZ",
     "registration": "LV-WIZ", "event_class": "Accident",
     "caso": None, "date_of_occurrence": "2015-11-01"},
]


class _FakeResp:
    def __init__(self, body=""):
        self.content = body.encode("utf-8") if isinstance(body, str) else body
        self.status_code = 200

    def raise_for_status(self):
        pass


class _FakeClient:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return _FakeResp("<page>")


# ── discover ──────────────────────────────────────────────────────────────────

def test_discover_inserts_new_rows(monkeypatch):
    conn = _conn()
    # parse_listing returns the same fake rows for every seed page; pdf_url
    # de-dup across pages must collapse them to 3 unique reports.
    monkeypatch.setattr(ciaiauy, "parse_listing", lambda html: list(_FAKE_ROWS))
    monkeypatch.setattr(ciaiauy, "DELAY", 0)
    client = _FakeClient()

    assert pipeline.discover(conn, client) == 3
    rows = conn.execute(
        "SELECT case_id, pdf_url, registration, event_class, date_of_occurrence, status, lang "
        "FROM ciaiauy_reports ORDER BY case_id"
    ).fetchall()
    assert len(rows) == 3
    assert all(r["status"] == db.STATUS_NEW for r in rows)
    assert all(r["lang"] == "es" for r in rows)

    by_cid = {r["case_id"]: r for r in rows}
    assert "caso-611" in by_cid
    assert by_cid["caso-611"]["event_class"] == "Serious incident"
    assert by_cid["caso-611"]["registration"] == "CX-OTA-R"
    assert "cx-mgp" in by_cid
    assert "lv-wiz" in by_cid
    assert by_cid["lv-wiz"]["date_of_occurrence"] == "2015-11-01"


def test_discover_idempotent(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(ciaiauy, "parse_listing", lambda html: list(_FAKE_ROWS))
    monkeypatch.setattr(ciaiauy, "DELAY", 0)
    client = _FakeClient()
    assert pipeline.discover(conn, client) == 3
    assert pipeline.discover(conn, client) == 0
    assert conn.execute("SELECT COUNT(*) FROM ciaiauy_reports").fetchone()[0] == 3


def test_discover_dedups_same_pdf_across_pages(monkeypatch):
    """The same pdf_url appearing on multiple seed pages inserts only once."""
    conn = _conn()
    one = [{"pdf_url": "https://www.gub.uy/x/dup.pdf", "title": "Informe Final CX-DUP",
            "registration": "CX-DUP", "event_class": "Accident",
            "caso": None, "date_of_occurrence": None}]
    monkeypatch.setattr(ciaiauy, "parse_listing", lambda html: list(one))
    monkeypatch.setattr(ciaiauy, "DELAY", 0)
    # 9 seed pages all yield the same single pdf -> 1 inserted row
    assert pipeline.discover(conn, _FakeClient()) == 1


def test_discover_case_id_collision_suffix(monkeypatch):
    """Two reports that normalise to the same base case_id get a -2 suffix."""
    conn = _conn()
    rows = [
        {"pdf_url": "https://www.gub.uy/x/a-611.pdf", "title": "611 A",
         "registration": "CX-AAA", "event_class": "Accident", "caso": "611",
         "date_of_occurrence": None},
        {"pdf_url": "https://www.gub.uy/x/b-611.pdf", "title": "611 B",
         "registration": "CX-BBB", "event_class": "Accident", "caso": "611",
         "date_of_occurrence": None},
    ]
    monkeypatch.setattr(ciaiauy, "parse_listing", lambda html: list(rows))
    monkeypatch.setattr(ciaiauy, "DELAY", 0)
    assert pipeline.discover(conn, _FakeClient()) == 2
    cids = {r["case_id"] for r in conn.execute("SELECT case_id FROM ciaiauy_reports")}
    assert cids == {"caso-611", "caso-611-2"}


# ── fetch ─────────────────────────────────────────────────────────────────────

def _seed_new(conn, case_id, pdf_url):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO ciaiauy_reports (case_id, pdf_url, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, pdf_url, db.STATUS_NEW, ts, ts),
    )
    conn.commit()


def test_fetch_downloads_pdf_and_advances(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "caso-611", "https://www.gub.uy/x/611.pdf")

    def _fake_download(client, url, dest):
        open(dest, "wb").write(b"%PDF")
    monkeypatch.setattr(ciaiauy, "download", _fake_download)
    monkeypatch.setattr(ciaiauy, "DELAY", 0)

    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    row = conn.execute(
        "SELECT status, pdf_path FROM ciaiauy_reports WHERE case_id='caso-611'"
    ).fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert os.path.exists(row["pdf_path"])


def test_fetch_download_failure_keeps_row_new(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "caso-611", "https://www.gub.uy/x/611.pdf")
    monkeypatch.setattr(
        ciaiauy, "download",
        lambda c, u, d: (_ for _ in ()).throw(RuntimeError("timeout")),
    )
    monkeypatch.setattr(ciaiauy, "DELAY", 0)
    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    assert conn.execute(
        "SELECT status FROM ciaiauy_reports WHERE case_id='caso-611'"
    ).fetchone()["status"] == db.STATUS_NEW


def test_fetch_isolates_per_row_errors(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "caso-611", "https://www.gub.uy/x/611.pdf")
    _seed_new(conn, "cx-mgp", "https://www.gub.uy/x/mgp.pdf")

    def _selective(client, url, dest):
        if "611" in url:
            raise RuntimeError("403")
        open(dest, "wb").write(b"%PDF")
    monkeypatch.setattr(ciaiauy, "download", _selective)
    monkeypatch.setattr(ciaiauy, "DELAY", 0)

    assert pipeline.fetch(conn, None, str(tmp_path)) == 2
    assert conn.execute("SELECT status FROM ciaiauy_reports WHERE case_id='caso-611'").fetchone()["status"] == db.STATUS_NEW
    assert conn.execute("SELECT status FROM ciaiauy_reports WHERE case_id='cx-mgp'").fetchone()["status"] == db.STATUS_FETCHED


# ── parse ─────────────────────────────────────────────────────────────────────

def _seed_fetched(conn, case_id, pdf_path="x.pdf"):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO ciaiauy_reports (case_id, status, pdf_path, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, db.STATUS_FETCHED, pdf_path, ts, ts),
    )
    conn.commit()


def test_parse_long_narrative_pdf_tier(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "caso-611")
    long_text = "X" * MIN_NARRATIVE
    monkeypatch.setattr(pipeline, "extract_text", lambda p: long_text)
    assert pipeline.parse(conn) == 1
    row = conn.execute("SELECT source_tier, narrative_text, status FROM ciaiauy_reports WHERE case_id='caso-611'").fetchone()
    assert row["source_tier"] == "pdf"
    assert row["status"] == db.STATUS_PARSED


def test_parse_scanned_tier(monkeypatch):
    """0 < len <= SCANNED_MAX -> scanned (image-only PDF)."""
    conn = _conn()
    _seed_fetched(conn, "caso-611")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "x" * (SCANNED_MAX - 1))
    pipeline.parse(conn)
    assert conn.execute("SELECT source_tier FROM ciaiauy_reports WHERE case_id='caso-611'").fetchone()["source_tier"] == "scanned"


def test_parse_short_tier(monkeypatch):
    """SCANNED_MAX < len < MIN_NARRATIVE -> short."""
    conn = _conn()
    _seed_fetched(conn, "caso-611")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "x" * (SCANNED_MAX + 1))
    pipeline.parse(conn)
    assert conn.execute("SELECT source_tier FROM ciaiauy_reports WHERE case_id='caso-611'").fetchone()["source_tier"] == "short"


def test_parse_none_tier(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "caso-611", pdf_path=None)
    calls = []
    monkeypatch.setattr(pipeline, "extract_text", lambda p: calls.append(p) or "x" * 1000)
    pipeline.parse(conn)
    assert not calls
    assert conn.execute("SELECT source_tier FROM ciaiauy_reports WHERE case_id='caso-611'").fetchone()["source_tier"] == "none"


# ── build ─────────────────────────────────────────────────────────────────────

def _seed_parsed(conn, case_id, *, narrative="", event_class="Accident",
                 registration=None, source_tier="pdf", pdf_url=None, report_url=None,
                 date=None):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO ciaiauy_reports "
        "(case_id, registration, date_of_occurrence, narrative_text, event_class, "
        "source_tier, pdf_url, report_url, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (case_id, registration, date, narrative, event_class, source_tier,
         pdf_url, report_url, db.STATUS_PARSED, ts, ts),
    )
    conn.commit()


def test_build_creates_accident_row():
    conn = _conn()
    _seed_parsed(conn, "caso-611", narrative="N" * 700, event_class="Serious incident",
                 registration="CX-OTA-R", pdf_url="https://www.gub.uy/x/611.pdf",
                 date="2016-01-01")
    assert pipeline.build(conn) == 1
    acc = conn.execute("SELECT * FROM ciaiauy_accidents WHERE case_id='caso-611'").fetchone()
    assert acc["country"] == "UY"
    assert acc["registration"] == "CX-OTA-R"
    assert acc["report_type"] == "Serious incident"
    assert acc["event_date"] == "2016-01-01"
    assert acc["source_url"] == "https://www.gub.uy/x/611.pdf"
    assert acc["site_slug"].startswith("crash-")
    assert conn.execute("SELECT status FROM ciaiauy_reports WHERE case_id='caso-611'").fetchone()["status"] == db.STATUS_BUILT


def test_build_skips_scanned():
    conn = _conn()
    _seed_parsed(conn, "caso-611", narrative="N" * 700, source_tier="scanned")
    assert pipeline.build(conn) == 0
    assert conn.execute("SELECT status FROM ciaiauy_reports WHERE case_id='caso-611'").fetchone()["status"] == db.STATUS_SKIPPED
    assert conn.execute("SELECT COUNT(*) FROM ciaiauy_accidents").fetchone()[0] == 0


def test_build_skips_below_floor():
    conn = _conn()
    _seed_parsed(conn, "caso-611", narrative="X" * 79, source_tier="short")
    assert pipeline.build(conn) == 0
    assert conn.execute("SELECT status FROM ciaiauy_reports WHERE case_id='caso-611'").fetchone()["status"] == db.STATUS_SKIPPED


def test_build_source_url_falls_back_to_report_url():
    conn = _conn()
    _seed_parsed(conn, "cx-mgp", narrative="N" * 200, pdf_url=None,
                 report_url="https://www.gub.uy/x/mgp.pdf")
    pipeline.build(conn)
    assert conn.execute("SELECT source_url FROM ciaiauy_accidents WHERE case_id='cx-mgp'").fetchone()["source_url"] == "https://www.gub.uy/x/mgp.pdf"


def test_build_country_is_uy():
    conn = _conn()
    _seed_parsed(conn, "cx-mgp", narrative="N" * 200)
    pipeline.build(conn)
    assert conn.execute("SELECT country FROM ciaiauy_accidents WHERE case_id='cx-mgp'").fetchone()["country"] == "UY"
