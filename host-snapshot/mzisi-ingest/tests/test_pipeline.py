# tests/test_pipeline.py
"""Pipeline tests for mzisi discover -> fetch -> parse -> build."""
import os

from mzisi_ingest import mzisi, db, pipeline
from mzisi_ingest.pdf import MIN_NARRATIVE
from mzisi_ingest.pipeline import SCANNED_FLOOR


def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


_FAKE_ROWS = [
    {
        "case_id": "mzisi-2024-koncno-a",
        "pdf_url": "https://www.gov.si/assets/.../2024/Koncno-A.pdf",
        "year": "2024",
        "registration": "S5-DLM",
        "title": "Koncno-A",
        "report_type": "Final report",
    },
    {
        "case_id": "mzisi-2022-povzetek-b",
        "pdf_url": "https://www.gov.si/assets/.../2022/Povzetek-B.pdf",
        "year": "2022",
        "registration": "OE-CYY",
        "title": "Povzetek-B",
        "report_type": "Summary",
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
    monkeypatch.setattr(mzisi, "parse_index", lambda html: _FAKE_ROWS)
    assert pipeline.discover(conn, _FakeClient()) == 2

    rows = conn.execute(
        "SELECT case_id, pdf_url, registration, event_class, status "
        "FROM mzisi_reports ORDER BY case_id"
    ).fetchall()
    assert len(rows) == 2
    assert all(r["status"] == db.STATUS_NEW for r in rows)
    r = next(r for r in rows if r["case_id"] == "mzisi-2024-koncno-a")
    assert r["pdf_url"].endswith("Koncno-A.pdf")
    assert r["registration"] == "S5-DLM"
    assert r["event_class"] == "Final report"


def test_discover_idempotent(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(mzisi, "parse_index", lambda html: _FAKE_ROWS)
    assert pipeline.discover(conn, _FakeClient()) == 2
    assert pipeline.discover(conn, _FakeClient()) == 0
    assert conn.execute("SELECT COUNT(*) FROM mzisi_reports").fetchone()[0] == 2


def test_discover_full_flag_accepted(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(mzisi, "parse_index", lambda html: _FAKE_ROWS)
    assert pipeline.discover(conn, _FakeClient(), full=True) == 2


# ── fetch ─────────────────────────────────────────────────────────────────────

def _seed_new(conn, case_id, pdf_url):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO mzisi_reports (case_id, pdf_url, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, pdf_url, db.STATUS_NEW, ts, ts),
    )
    conn.commit()


def test_fetch_downloads_pdf_and_advances(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "mzisi-2024-a", "https://www.gov.si/x/Koncno.pdf")

    calls = []
    def _dl(client, url, dest):
        calls.append((url, dest))
        open(dest, "wb").write(b"%PDF")
    monkeypatch.setattr(mzisi, "download", _dl)
    monkeypatch.setattr(mzisi, "DELAY", 0)

    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    row = conn.execute(
        "SELECT status, pdf_path FROM mzisi_reports WHERE case_id='mzisi-2024-a'"
    ).fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert os.path.exists(row["pdf_path"])
    assert len(calls) == 1


def test_fetch_slash_safe_filename(monkeypatch, tmp_path):
    """case_id never has a slash at the staging stage, but path stays safe."""
    conn = _conn()
    _seed_new(conn, "mzisi-2024-a", "https://www.gov.si/x/Koncno.pdf")
    monkeypatch.setattr(mzisi, "download", lambda c, u, d: open(d, "wb").write(b"%PDF"))
    monkeypatch.setattr(mzisi, "DELAY", 0)
    pipeline.fetch(conn, None, str(tmp_path))
    row = conn.execute("SELECT pdf_path FROM mzisi_reports").fetchone()
    assert "/" not in os.path.basename(row["pdf_path"]).replace(".pdf", "").replace("mzisi-2024-a", "")


def test_fetch_download_failure_keeps_row_new(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "mzisi-2024-a", "https://www.gov.si/x/Koncno.pdf")
    monkeypatch.setattr(
        mzisi, "download",
        lambda c, u, d: (_ for _ in ()).throw(RuntimeError("timeout")),
    )
    monkeypatch.setattr(mzisi, "DELAY", 0)
    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    assert conn.execute(
        "SELECT status FROM mzisi_reports WHERE case_id='mzisi-2024-a'"
    ).fetchone()["status"] == db.STATUS_NEW


def test_fetch_isolates_per_row_errors(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "mzisi-2024-a", "https://www.gov.si/x/a.pdf")
    _seed_new(conn, "mzisi-2022-b", "https://www.gov.si/x/b.pdf")
    def _sel(client, url, dest):
        if "a.pdf" in url:
            raise RuntimeError("403")
        open(dest, "wb").write(b"%PDF")
    monkeypatch.setattr(mzisi, "download", _sel)
    monkeypatch.setattr(mzisi, "DELAY", 0)
    assert pipeline.fetch(conn, None, str(tmp_path)) == 2
    assert conn.execute(
        "SELECT status FROM mzisi_reports WHERE case_id='mzisi-2024-a'"
    ).fetchone()["status"] == db.STATUS_NEW
    assert conn.execute(
        "SELECT status FROM mzisi_reports WHERE case_id='mzisi-2022-b'"
    ).fetchone()["status"] == db.STATUS_FETCHED


# ── parse ─────────────────────────────────────────────────────────────────────

def _seed_fetched(conn, case_id, pdf_path=None):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO mzisi_reports (case_id, status, pdf_path, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, db.STATUS_FETCHED, pdf_path, ts, ts),
    )
    conn.commit()


def test_parse_long_narrative_pdf_tier(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "mzisi-2024-a", pdf_path="a.pdf")
    long_text = "Letalske nesrece " * 60 + " 37200-6/2016 "
    monkeypatch.setattr(pipeline, "extract_text", lambda p: long_text)
    assert pipeline.parse(conn) == 1
    row = conn.execute(
        "SELECT narrative_text, source_tier, source_event_id, lang, status "
        "FROM mzisi_reports WHERE case_id='mzisi-2024-a'"
    ).fetchone()
    assert row["status"] == db.STATUS_PARSED
    assert row["source_tier"] == "pdf"
    assert row["source_event_id"] == "37200-6/2016"
    assert row["lang"] == "sl"


def test_parse_short_tier(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "mzisi-2024-a", pdf_path="a.pdf")
    text = "X" * (SCANNED_FLOOR + 10)
    monkeypatch.setattr(pipeline, "extract_text", lambda p: text)
    pipeline.parse(conn)
    assert conn.execute(
        "SELECT source_tier FROM mzisi_reports WHERE case_id='mzisi-2024-a'"
    ).fetchone()["source_tier"] == "short"


def test_parse_scanned_tier(monkeypatch):
    """Below SCANNED_FLOOR but non-empty → tier='scanned' (image-only PDF)."""
    conn = _conn()
    _seed_fetched(conn, "mzisi-2006-cam", pdf_path="cam.pdf")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "x" * 19)
    pipeline.parse(conn)
    assert conn.execute(
        "SELECT source_tier FROM mzisi_reports WHERE case_id='mzisi-2006-cam'"
    ).fetchone()["source_tier"] == "scanned"


def test_parse_no_pdf_path_none_tier(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "mzisi-2024-x", pdf_path=None)
    calls = []
    monkeypatch.setattr(pipeline, "extract_text", lambda p: calls.append(p) or "Y" * 1000)
    pipeline.parse(conn)
    assert not calls
    assert conn.execute(
        "SELECT source_tier FROM mzisi_reports WHERE case_id='mzisi-2024-x'"
    ).fetchone()["source_tier"] == "none"


# ── build ─────────────────────────────────────────────────────────────────────

def _seed_parsed(conn, case_id, *, source_event_id=None, aircraft=None,
                 registration=None, location=None, date=None, narrative="",
                 event_class=None, operator=None, pdf_url=None, report_url=None):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO mzisi_reports "
        "(case_id, source_event_id, aircraft, registration, location, "
        "date_of_occurrence, narrative_text, event_class, operator, pdf_url, "
        "report_url, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (case_id, source_event_id, aircraft, registration, location, date,
         narrative, event_class, operator, pdf_url, report_url,
         db.STATUS_PARSED, ts, ts),
    )
    conn.commit()


def test_build_uses_source_event_id_as_case_id():
    conn = _conn()
    _seed_parsed(
        conn, "mzisi-2016-koncno-s5-des",
        source_event_id="37200-6/2016",
        aircraft="Piper PA-28", registration="S5-DES",
        location="Maribor", date="2016-05-01",
        narrative="N" * 300, event_class="Final report",
        pdf_url="https://www.gov.si/x/koncno.pdf",
    )
    assert pipeline.build(conn) == 1
    acc = conn.execute("SELECT * FROM mzisi_accidents").fetchone()
    assert acc["case_id"] == "37200-6/2016"   # raw, slash preserved
    assert "/" in acc["case_id"]
    assert acc["country"] == "SI"
    assert acc["report_type"] == "Final report"
    assert acc["source_url"] == "https://www.gov.si/x/koncno.pdf"
    assert acc["site_slug"].startswith("crash-")
    assert conn.execute(
        "SELECT status FROM mzisi_reports WHERE case_id='mzisi-2016-koncno-s5-des'"
    ).fetchone()["status"] == db.STATUS_BUILT


def test_build_falls_back_to_staging_case_id_when_no_source_event_id():
    conn = _conn()
    _seed_parsed(
        conn, "mzisi-2020-summary",
        source_event_id=None,
        narrative="N" * 200, event_class="Summary",
        pdf_url="https://www.gov.si/x/povzetek.pdf",
    )
    pipeline.build(conn)
    acc = conn.execute("SELECT case_id FROM mzisi_accidents").fetchone()
    assert acc["case_id"] == "mzisi-2020-summary"


def test_build_skips_below_floor():
    conn = _conn()
    _seed_parsed(conn, "mzisi-2006-cam", narrative="x" * 19, event_class="Final report")
    assert pipeline.build(conn) == 0
    assert conn.execute(
        "SELECT status FROM mzisi_reports WHERE case_id='mzisi-2006-cam'"
    ).fetchone()["status"] == db.STATUS_SKIPPED
    assert conn.execute("SELECT COUNT(*) FROM mzisi_accidents").fetchone()[0] == 0


def test_build_country_is_si():
    conn = _conn()
    _seed_parsed(conn, "mzisi-2024-a", narrative="N" * 200, event_class="Final report")
    pipeline.build(conn)
    assert conn.execute(
        "SELECT country FROM mzisi_accidents"
    ).fetchone()["country"] == "SI"


def test_build_mixed_rows():
    conn = _conn()
    long_n = "Z" * 200
    _seed_parsed(conn, "mzisi-1", source_event_id="37200-1/2024", narrative=long_n,
                 event_class="Final report")
    _seed_parsed(conn, "mzisi-2", source_event_id="37200-2/2024", narrative=long_n,
                 event_class="Summary")
    _seed_parsed(conn, "mzisi-3", narrative="", event_class="Final report")
    assert pipeline.build(conn) == 2
    assert conn.execute("SELECT COUNT(*) FROM mzisi_accidents").fetchone()[0] == 2


# ── parse persists extracted event_date / location / aircraft (C1) ───────────

def test_parse_extracts_event_date_location_aircraft(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "mzisi-2016-koncno-s5-des", pdf_path="a.pdf")
    cover = (
        "Številka:\nDatum:\n\n37200-6/2016-2430-39\n25. 1. 2020\n\n"
        "KONČNO POROČILO\nmotornega letala PIPER PA-28-161,\n"
        "v bližini letališča BOVEC – LJBO\n1. septembra 2016\n"
        + ("dejstva o letu " * 60)
    )
    monkeypatch.setattr(pipeline, "extract_text", lambda p: cover)
    assert pipeline.parse(conn) == 1
    row = conn.execute(
        "SELECT date_of_occurrence, location, aircraft "
        "FROM mzisi_reports WHERE case_id='mzisi-2016-koncno-s5-des'"
    ).fetchone()
    assert row["date_of_occurrence"] == "2016-09-01"   # publication 2020 ignored
    assert row["location"] == "letališča BOVEC – LJBO"
    assert row["aircraft"] == "PIPER PA-28-161"


def test_parse_event_date_null_when_scanned(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "mzisi-2006-cam", pdf_path="cam.pdf")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "x" * 19)  # no dates
    pipeline.parse(conn)
    row = conn.execute(
        "SELECT date_of_occurrence, location, aircraft "
        "FROM mzisi_reports WHERE case_id='mzisi-2006-cam'"
    ).fetchone()
    assert row["date_of_occurrence"] is None
    assert row["location"] is None
    assert row["aircraft"] is None


def test_parse_to_build_populates_accident_event_date(monkeypatch):
    """End-to-end: extracted event_date flows into mzisi_accidents.event_date."""
    conn = _conn()
    _seed_fetched(conn, "mzisi-2015-x", pdf_path="a.pdf")
    cover = (
        "KONČNO POROČILO\nMOTORNEGA LETALA PIPER PA28R-201,\n"
        "reg. oznake OE-DYM, v kraju Mengeš,\n3. 12. 2015\n37200-9/2015\n"
        + ("besedilo " * 60)
    )
    monkeypatch.setattr(pipeline, "extract_text", lambda p: cover)
    pipeline.parse(conn)
    pipeline.build(conn)
    acc = conn.execute("SELECT * FROM mzisi_accidents").fetchone()
    assert acc["event_date"] == "2015-12-03"
    assert acc["location"] == "Mengeš"
    assert acc["aircraft"] == "PIPER PA28R-201"
    assert acc["case_id"] == "37200-9/2015"


# ── I2: accident_case_id is immutable once assigned ──────────────────────────

def test_build_freezes_accident_case_id_on_first_build():
    conn = _conn()
    _seed_parsed(conn, "mzisi-2020-summary", source_event_id=None,
                 narrative="N" * 200, event_class="Summary",
                 pdf_url="https://www.gov.si/x/povzetek.pdf")
    pipeline.build(conn)
    frozen = conn.execute(
        "SELECT accident_case_id FROM mzisi_reports WHERE case_id='mzisi-2020-summary'"
    ).fetchone()["accident_case_id"]
    assert frozen == "mzisi-2020-summary"   # staging-slug, persisted


def test_reparse_newly_extracted_3720x_does_not_change_accident_key():
    """
    A report first built with NO official number (staging-slug key); a later
    re-parse extracts a 3720X number. The next build MUST keep the original key,
    not mint a new mzisi_accidents row.
    """
    conn = _conn()
    # First build: no source_event_id → staging-slug key, frozen.
    _seed_parsed(conn, "mzisi-2020-summary", source_event_id=None,
                 narrative="N" * 200, event_class="Summary")
    assert pipeline.build(conn) == 1
    assert conn.execute("SELECT case_id FROM mzisi_accidents").fetchone()["case_id"] \
        == "mzisi-2020-summary"

    # Simulate a re-parse that NOW extracts an official number and re-queues the
    # row for build (status back to parsed, source_event_id populated).
    conn.execute(
        "UPDATE mzisi_reports SET source_event_id='37200-9/2020', status=? "
        "WHERE case_id='mzisi-2020-summary'",
        (db.STATUS_PARSED,),
    )
    conn.commit()
    assert pipeline.build(conn) == 1

    # Still exactly ONE accident row, under the ORIGINAL (frozen) key.
    rows = conn.execute("SELECT case_id FROM mzisi_accidents").fetchall()
    assert len(rows) == 1
    assert rows[0]["case_id"] == "mzisi-2020-summary"   # unchanged, not 37200-9/2020
