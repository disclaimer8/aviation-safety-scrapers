# tests/test_pipeline.py
"""Pipeline tests for cins discover -> fetch -> parse -> build."""
import os

from cins_ingest import cins, db, pipeline
from cins_ingest.pdf import MIN_NARRATIVE


def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


_FAKE_ROWS = [
    {
        "case_id": "01-24",
        "report_url": None,
        "pdf_url": "https://arhiva.cins.gov.rs/doc/vazdusni-saobracaj/2024/01-24-2.PDF",
        "pdf_url_es": None, "pdf_url_en": None,
        "event_class": "Accident",
        "aircraft": "Embraer E190-200LR",
        "registration": "OY-GDC",
        "date_of_occurrence": "2024-02-18",
        "location": "Aerodrom Nikola Tesla, Beograd",
        "title": "01-24 | Embraer | Udes",
    },
    {
        "case_id": "02-23",
        "report_url": None,
        "pdf_url": "https://arhiva.cins.gov.rs/doc/vazdusni-saobracaj/2023/02-23.pdf",
        "pdf_url_es": None, "pdf_url_en": None,
        "event_class": "Accident",
        "aircraft": "Esqual VM 1C",
        "registration": "YU-A299",
        "date_of_occurrence": "2023-08-10",
        "location": "Batajnica, Beograd",
        "title": "02-23 | Esqual | Udes",
    },
]


class _FakeResp:
    def __init__(self, body=""):
        self.content = body.encode("utf-8") if isinstance(body, str) else body
        self.status_code = 200

    def raise_for_status(self):
        pass


class _FakeClient:
    def __init__(self, index_html="<html></html>"):
        self._index_html = index_html
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return _FakeResp(self._index_html)


# ── discover ───────────────────────────────────────────────────────────────

def test_discover_inserts_new_rows(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(cins, "parse_listing", lambda html: _FAKE_ROWS)
    assert pipeline.discover(conn, _FakeClient()) == 2

    rows = conn.execute(
        "SELECT case_id, pdf_url, status, event_class, aircraft, registration, "
        "date_of_occurrence, location, report_url FROM cins_reports ORDER BY case_id"
    ).fetchall()
    assert len(rows) == 2
    assert all(r["status"] == db.STATUS_NEW for r in rows)

    r = next(x for x in rows if x["case_id"] == "01-24")
    assert r["pdf_url"].endswith("01-24-2.PDF")
    assert r["event_class"] == "Accident"
    assert r["aircraft"] == "Embraer E190-200LR"
    assert r["registration"] == "OY-GDC"
    assert r["date_of_occurrence"] == "2024-02-18"
    assert r["report_url"] is None


def test_discover_idempotent(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(cins, "parse_listing", lambda html: _FAKE_ROWS)
    assert pipeline.discover(conn, _FakeClient()) == 2
    assert pipeline.discover(conn, _FakeClient()) == 0
    assert conn.execute("SELECT COUNT(*) FROM cins_reports").fetchone()[0] == 2


def test_discover_full_flag_accepted(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(cins, "parse_listing", lambda html: _FAKE_ROWS)
    assert pipeline.discover(conn, _FakeClient(), full=True) == 2


# ── fetch ──────────────────────────────────────────────────────────────────

def _seed_new(conn, case_id, pdf_url):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO cins_reports (case_id, pdf_url, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, pdf_url, db.STATUS_NEW, ts, ts),
    )
    conn.commit()


def test_fetch_downloads_pdf_and_advances(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "01-24", "https://arhiva.cins.gov.rs/x/01-24.pdf")

    calls = []

    def _fake_download(client, url, dest):
        calls.append((url, dest))
        open(dest, "wb").write(b"%PDF")

    monkeypatch.setattr(cins, "download", _fake_download)
    monkeypatch.setattr(cins, "DELAY", 0)

    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    row = conn.execute("SELECT status, pdf_path FROM cins_reports WHERE case_id='01-24'").fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert os.path.exists(row["pdf_path"])
    assert len(calls) == 1


def test_fetch_download_failure_keeps_row_new(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "01-24", "https://arhiva.cins.gov.rs/x/01-24.pdf")
    monkeypatch.setattr(
        cins, "download",
        lambda client, url, dest: (_ for _ in ()).throw(RuntimeError("timeout")),
    )
    monkeypatch.setattr(cins, "DELAY", 0)
    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    assert conn.execute(
        "SELECT status FROM cins_reports WHERE case_id='01-24'"
    ).fetchone()["status"] == db.STATUS_NEW


def test_fetch_isolates_per_row_errors(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "01-24", "https://arhiva.cins.gov.rs/x/01-24.pdf")
    _seed_new(conn, "02-23", "https://arhiva.cins.gov.rs/x/02-23.pdf")

    def _selective(client, url, dest):
        if "01-24" in url:
            raise RuntimeError("403")
        open(dest, "wb").write(b"%PDF")

    monkeypatch.setattr(cins, "download", _selective)
    monkeypatch.setattr(cins, "DELAY", 0)
    assert pipeline.fetch(conn, None, str(tmp_path)) == 2
    assert conn.execute("SELECT status FROM cins_reports WHERE case_id='01-24'").fetchone()["status"] == db.STATUS_NEW
    assert conn.execute("SELECT status FROM cins_reports WHERE case_id='02-23'").fetchone()["status"] == db.STATUS_FETCHED


# ── parse (incl. mojibake gate) ────────────────────────────────────────────

def _seed_fetched(conn, case_id, pdf_path="x.pdf"):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO cins_reports (case_id, status, pdf_path, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, db.STATUS_FETCHED, pdf_path, ts, ts),
    )
    conn.commit()


def test_parse_long_cyrillic_narrative(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "01-24")
    long_text = ("Извештај о истрази удеса ваздухоплова. " * 40)
    monkeypatch.setattr(pipeline, "extract_text", lambda p: long_text)
    assert pipeline.parse(conn) == 1
    row = conn.execute(
        "SELECT narrative_text, source_tier, lang, status FROM cins_reports WHERE case_id='01-24'"
    ).fetchone()
    assert row["status"] == db.STATUS_PARSED
    assert row["source_tier"] == "pdf"
    assert row["lang"] == "sr"
    assert row["narrative_text"] == long_text


def test_parse_mojibake_classified_scanned_and_dropped(monkeypatch):
    """Garbled embedded-font text → tier='scanned', narrative cleared."""
    conn = _conn()
    _seed_fetched(conn, "01-24")
    mojibake = "PEIIYEJII4KA CPBI4JA TIEHTAP 34 14CTPAxtI4BAISE HECPEhA Y CAOEPAhAJY " * 30
    monkeypatch.setattr(pipeline, "extract_text", lambda p: mojibake)
    assert pipeline.parse(conn) == 1
    row = conn.execute(
        "SELECT narrative_text, source_tier FROM cins_reports WHERE case_id='01-24'"
    ).fetchone()
    assert row["source_tier"] == "scanned"
    assert row["narrative_text"] == ""


def test_parse_empty_is_none(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "01-24", pdf_path=None)
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "X" * 999)
    assert pipeline.parse(conn) == 1
    row = conn.execute(
        "SELECT source_tier, narrative_text FROM cins_reports WHERE case_id='01-24'"
    ).fetchone()
    assert row["source_tier"] == "none"
    assert row["narrative_text"] == ""


def test_parse_short_usable_tier(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "01-24")
    short = "Izvestaj o udesu aviona na aerodromu."  # usable but short
    monkeypatch.setattr(pipeline, "extract_text", lambda p: short)
    pipeline.parse(conn)
    row = conn.execute(
        "SELECT source_tier, narrative_text FROM cins_reports WHERE case_id='01-24'"
    ).fetchone()
    assert row["source_tier"] == "short"
    assert row["narrative_text"] == short


# ── build ──────────────────────────────────────────────────────────────────

def _seed_parsed(conn, case_id, *, aircraft=None, registration=None,
                 location=None, date=None, narrative="", event_class=None,
                 pdf_url=None, report_url=None):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO cins_reports "
        "(case_id, aircraft, registration, location, date_of_occurrence, "
        "narrative_text, event_class, pdf_url, report_url, "
        "status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (case_id, aircraft, registration, location, date, narrative,
         event_class, pdf_url, report_url, db.STATUS_PARSED, ts, ts),
    )
    conn.commit()


def test_build_creates_accident_row():
    conn = _conn()
    narr = "Н" * 200
    _seed_parsed(conn, "01-24", aircraft="Embraer E190", registration="OY-GDC",
                 location="Beograd", date="2024-02-18", narrative=narr,
                 event_class="Accident",
                 pdf_url="https://arhiva.cins.gov.rs/x/01-24.pdf")
    assert pipeline.build(conn) == 1
    acc = conn.execute("SELECT * FROM cins_accidents WHERE case_id='01-24'").fetchone()
    assert acc["country"] == "RS"
    assert acc["event_date"] == "2024-02-18"
    assert acc["registration"] == "OY-GDC"
    assert acc["report_type"] == "Accident"
    assert acc["narrative_text"] == narr
    assert acc["probable_cause"] is None
    assert acc["source_url"].endswith("01-24.pdf")
    assert acc["site_slug"].startswith("crash-")
    assert conn.execute(
        "SELECT status FROM cins_reports WHERE case_id='01-24'"
    ).fetchone()["status"] == db.STATUS_BUILT


def test_build_skips_empty_narrative():
    conn = _conn()
    _seed_parsed(conn, "01-24", aircraft="X", narrative="", event_class="Accident")
    assert pipeline.build(conn) == 0
    assert conn.execute(
        "SELECT status FROM cins_reports WHERE case_id='01-24'"
    ).fetchone()["status"] == db.STATUS_SKIPPED
    assert conn.execute("SELECT COUNT(*) FROM cins_accidents").fetchone()[0] == 0


def test_build_skips_below_floor():
    conn = _conn()
    _seed_parsed(conn, "01-24", narrative="X" * 79, event_class="Accident")
    assert pipeline.build(conn) == 0


def test_build_country_is_rs():
    conn = _conn()
    _seed_parsed(conn, "01-24", narrative="N" * 200, event_class="Accident")
    pipeline.build(conn)
    assert conn.execute(
        "SELECT country FROM cins_accidents WHERE case_id='01-24'"
    ).fetchone()["country"] == "RS"
