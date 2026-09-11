"""Pipeline tests for aaiubg discover -> fetch -> parse -> build."""
import os

from aaiubg_ingest import aaiubg, db, pipeline
from aaiubg_ingest.pdf import MIN_NARRATIVE


def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


# Fake listing rows returned by parse_listing mock:
#   row0: reg+date case_id, full metadata
#   row1: airprox 'D Month YYYY' date row
#   row2: no registration -> filename-fallback case_id
_FAKE_ROWS = [
    {
        "case_id": "LZ-PTS_2022-08-08",
        "pdf_url": "https://www.mtc.government.bg/x/FR_EN_LZPTS.pdf",
        "pdf_filename": "FR_EN_LZPTS.pdf",
        "event_class": "Accident",
        "aircraft": "Partenavia P.66C",
        "registration": "LZ-PTS",
        "date_of_occurrence": "2022-08-08",
        "operator": "pilot owner",
        "title": "Final report ... accident ... LZ-PTS ... 08.08.2022 ...",
    },
    {
        "case_id": "D-ASXP_2018-08-12",
        "pdf_url": "https://www.mtc.government.bg/x/airprox.pdf",
        "pdf_filename": "airprox.pdf",
        "event_class": "Serious incident",
        "aircraft": "Boeing B737-800",
        "registration": "D-ASXP",
        "date_of_occurrence": "2018-08-12",
        "operator": "SunExpress Deutschland GmbH",
        "title": "... serious incident (Airprox) ... D-ASXP ... on 12 August 2018 ...",
    },
    {
        "case_id": "bg-report-r22-eng-ft",
        "pdf_url": "https://www.mtc.government.bg/x/final_report_r22_eng_ft.pdf",
        "pdf_filename": "final_report_r22_eng_ft.pdf",
        "event_class": "Aviation event",
        "aircraft": "R22 BETA",
        "registration": None,
        "date_of_occurrence": "2015-08-02",
        "operator": "its owner",
        "title": "... aviation event ... helicopter R22 BETA, without registration marks ...",
    },
]

_YEAR_URL = "https://www.mtc.government.bg/en/category/193/x-2022"


class _FakeResp:
    def __init__(self, body=""):
        self.content = body.encode("utf-8") if isinstance(body, str) else body
        self.status_code = 200

    def raise_for_status(self):
        pass


class _FakeClient:
    def __init__(self, index_html="<index>", year_html="<year>"):
        self._index_html = index_html
        self._year_html = year_html
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if url == aaiubg.INDEX_URL:
            return _FakeResp(self._index_html)
        return _FakeResp(self._year_html)


# ── discover ────────────────────────────────────────────────────────────────

def test_discover_inserts_new_rows(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(aaiubg, "iter_year_urls", lambda html: [_YEAR_URL])
    monkeypatch.setattr(aaiubg, "parse_listing", lambda html, year_url="": _FAKE_ROWS)
    monkeypatch.setattr(aaiubg, "DELAY", 0)
    client = _FakeClient()

    assert pipeline.discover(conn, client) == 3

    rows = conn.execute(
        "SELECT case_id, pdf_url, pdf_filename, lang, status, event_class, "
        "aircraft, registration, date_of_occurrence, operator, report_url "
        "FROM aaiubg_reports ORDER BY case_id"
    ).fetchall()
    assert len(rows) == 3
    assert all(r["status"] == db.STATUS_NEW for r in rows)
    # all English PDFs -> lang='en'
    assert all(r["lang"] == "en" for r in rows)

    r = next(r for r in rows if r["case_id"] == "LZ-PTS_2022-08-08")
    assert r["registration"] == "LZ-PTS"
    assert r["date_of_occurrence"] == "2022-08-08"
    assert r["event_class"] == "Accident"
    assert r["operator"] == "pilot owner"
    assert r["report_url"] == _YEAR_URL  # year page recorded as report_url

    r_noreg = next(r for r in rows if r["case_id"] == "bg-report-r22-eng-ft")
    assert r_noreg["registration"] is None


def test_discover_idempotent(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(aaiubg, "iter_year_urls", lambda html: [_YEAR_URL])
    monkeypatch.setattr(aaiubg, "parse_listing", lambda html, year_url="": _FAKE_ROWS)
    monkeypatch.setattr(aaiubg, "DELAY", 0)
    client = _FakeClient()

    assert pipeline.discover(conn, client) == 3
    assert pipeline.discover(conn, client) == 0
    assert conn.execute("SELECT COUNT(*) FROM aaiubg_reports").fetchone()[0] == 3


def test_discover_skips_existing_case_id(monkeypatch):
    conn = _conn()
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO aaiubg_reports (case_id, status, discovered_at, updated_at) VALUES (?,?,?,?)",
        ("LZ-PTS_2022-08-08", db.STATUS_NEW, ts, ts),
    )
    conn.commit()
    monkeypatch.setattr(aaiubg, "iter_year_urls", lambda html: [_YEAR_URL])
    monkeypatch.setattr(aaiubg, "parse_listing", lambda html, year_url="": _FAKE_ROWS)
    monkeypatch.setattr(aaiubg, "DELAY", 0)
    assert pipeline.discover(conn, _FakeClient()) == 2


def test_discover_full_flag_accepted(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(aaiubg, "iter_year_urls", lambda html: [_YEAR_URL])
    monkeypatch.setattr(aaiubg, "parse_listing", lambda html, year_url="": _FAKE_ROWS)
    monkeypatch.setattr(aaiubg, "DELAY", 0)
    assert pipeline.discover(conn, _FakeClient(), full=True) == 3


# ── fetch ─────────────────────────────────────────────────────────────────────

def _seed_new(conn, case_id, pdf_url):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO aaiubg_reports (case_id, pdf_url, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, pdf_url, db.STATUS_NEW, ts, ts),
    )
    conn.commit()


def test_fetch_downloads_pdf_and_advances(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "LZ-PTS_2022-08-08", "https://x/lz-pts.pdf")
    calls = []

    def _fake_download(client, url, dest):
        calls.append((url, dest))
        open(dest, "wb").write(b"%PDF")

    monkeypatch.setattr(aaiubg, "download", _fake_download)
    monkeypatch.setattr(aaiubg, "DELAY", 0)

    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    row = conn.execute(
        "SELECT status, pdf_path FROM aaiubg_reports WHERE case_id='LZ-PTS_2022-08-08'"
    ).fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert os.path.exists(row["pdf_path"])
    assert len(calls) == 1


def test_fetch_no_pdf_url_advances_with_null_path(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "bg-x", None)
    calls = []
    monkeypatch.setattr(aaiubg, "download", lambda *a: calls.append(a))
    monkeypatch.setattr(aaiubg, "DELAY", 0)

    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    assert not calls
    row = conn.execute("SELECT status, pdf_path FROM aaiubg_reports WHERE case_id='bg-x'").fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert row["pdf_path"] is None


def test_fetch_download_failure_keeps_row_new(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "LZ-PTS_2022-08-08", "https://x/lz-pts.pdf")
    monkeypatch.setattr(
        aaiubg, "download",
        lambda client, url, dest: (_ for _ in ()).throw(RuntimeError("timeout")),
    )
    monkeypatch.setattr(aaiubg, "DELAY", 0)
    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    assert conn.execute(
        "SELECT status FROM aaiubg_reports WHERE case_id='LZ-PTS_2022-08-08'"
    ).fetchone()["status"] == db.STATUS_NEW


def test_fetch_isolates_per_row_errors(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "LZ-PTS_2022-08-08", "https://x/lz-pts.pdf")
    _seed_new(conn, "G-EZBV_2022-11-17", "https://x/g-ezbv.pdf")

    def _selective(client, url, dest):
        if "lz-pts" in url:
            raise RuntimeError("403 Forbidden")
        open(dest, "wb").write(b"%PDF")

    monkeypatch.setattr(aaiubg, "download", _selective)
    monkeypatch.setattr(aaiubg, "DELAY", 0)
    assert pipeline.fetch(conn, None, str(tmp_path)) == 2
    assert conn.execute(
        "SELECT status FROM aaiubg_reports WHERE case_id='LZ-PTS_2022-08-08'"
    ).fetchone()["status"] == db.STATUS_NEW
    assert conn.execute(
        "SELECT status FROM aaiubg_reports WHERE case_id='G-EZBV_2022-11-17'"
    ).fetchone()["status"] == db.STATUS_FETCHED


# ── parse ─────────────────────────────────────────────────────────────────────

def _seed_fetched(conn, case_id, pdf_path=None):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO aaiubg_reports (case_id, status, pdf_path, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, db.STATUS_FETCHED, pdf_path, ts, ts),
    )
    conn.commit()


def test_parse_long_narrative(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "LZ-PTS_2022-08-08", pdf_path="x.pdf")
    long_text = "X" * MIN_NARRATIVE
    monkeypatch.setattr(pipeline, "extract_text", lambda p: long_text)
    assert pipeline.parse(conn) == 1
    row = conn.execute(
        "SELECT narrative_text, source_tier, status FROM aaiubg_reports WHERE case_id='LZ-PTS_2022-08-08'"
    ).fetchone()
    assert row["status"] == db.STATUS_PARSED
    assert row["source_tier"] == "pdf"
    assert row["narrative_text"] == long_text


def test_parse_short_scanned_tier(monkeypatch):
    """Below MIN_NARRATIVE but non-empty (scanned-image-ish) -> 'short'."""
    conn = _conn()
    _seed_fetched(conn, "LZ-PTS_2022-08-08", pdf_path="x.pdf")
    short_text = "scanned page with little text " * 3
    monkeypatch.setattr(pipeline, "extract_text", lambda p: short_text)
    pipeline.parse(conn)
    assert conn.execute(
        "SELECT source_tier FROM aaiubg_reports WHERE case_id='LZ-PTS_2022-08-08'"
    ).fetchone()["source_tier"] == "short"


def test_parse_no_pdf_path(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "bg-x", pdf_path=None)
    calls = []
    monkeypatch.setattr(pipeline, "extract_text", lambda p: calls.append(p) or "X" * 1000)
    assert pipeline.parse(conn) == 1
    assert not calls
    row = conn.execute("SELECT source_tier, narrative_text FROM aaiubg_reports WHERE case_id='bg-x'").fetchone()
    assert row["source_tier"] == "none"
    assert row["narrative_text"] == ""


def test_parse_empty_extraction(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "LZ-PTS_2022-08-08", pdf_path="x.pdf")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "")
    pipeline.parse(conn)
    assert conn.execute(
        "SELECT source_tier FROM aaiubg_reports WHERE case_id='LZ-PTS_2022-08-08'"
    ).fetchone()["source_tier"] == "none"


# ── build ─────────────────────────────────────────────────────────────────────

def _seed_parsed(conn, case_id, *, aircraft=None, registration=None,
                 location=None, date=None, narrative="", event_class=None,
                 operator=None, pdf_url=None, report_url=None):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO aaiubg_reports "
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
    _seed_parsed(conn, "LZ-PTS_2022-08-08", aircraft="Partenavia P.66C",
                 registration="LZ-PTS", location=None, date="2022-08-08",
                 narrative=narr, event_class="Accident", operator="pilot owner",
                 pdf_url="https://x/lz-pts.pdf")
    assert pipeline.build(conn) == 1
    acc = conn.execute("SELECT * FROM aaiubg_accidents WHERE case_id='LZ-PTS_2022-08-08'").fetchone()
    assert acc["country"] == "BG"
    assert acc["event_date"] == "2022-08-08"
    assert acc["aircraft"] == "Partenavia P.66C"
    assert acc["registration"] == "LZ-PTS"
    assert acc["operator"] == "pilot owner"
    assert acc["report_type"] == "Accident"
    assert acc["narrative_text"] == narr
    assert acc["probable_cause"] is None
    assert acc["source_url"] == "https://x/lz-pts.pdf"
    assert acc["site_slug"] == "lz-pts-2022-08-08"
    assert conn.execute(
        "SELECT status FROM aaiubg_reports WHERE case_id='LZ-PTS_2022-08-08'"
    ).fetchone()["status"] == db.STATUS_BUILT


def test_build_source_url_falls_back_to_report_url():
    conn = _conn()
    _seed_parsed(conn, "bg-x", narrative="N" * 200, event_class="Accident",
                 pdf_url=None, report_url="https://www.mtc.government.bg/en/category/193/x")
    pipeline.build(conn)
    acc = conn.execute("SELECT source_url FROM aaiubg_accidents WHERE case_id='bg-x'").fetchone()
    assert acc["source_url"] == "https://www.mtc.government.bg/en/category/193/x"


def test_build_skips_empty_narrative():
    conn = _conn()
    _seed_parsed(conn, "LZ-X_2020-01-01", narrative="", event_class="Accident",
                 pdf_url="https://x/x.pdf")
    assert pipeline.build(conn) == 0
    assert conn.execute(
        "SELECT status FROM aaiubg_reports WHERE case_id='LZ-X_2020-01-01'"
    ).fetchone()["status"] == db.STATUS_SKIPPED
    assert conn.execute("SELECT COUNT(*) FROM aaiubg_accidents").fetchone()[0] == 0


def test_build_skips_below_narrative_floor():
    conn = _conn()
    _seed_parsed(conn, "LZ-Y_2020-01-01", narrative="X" * 79, event_class="Serious incident")
    assert pipeline.build(conn) == 0
    assert conn.execute(
        "SELECT status FROM aaiubg_reports WHERE case_id='LZ-Y_2020-01-01'"
    ).fetchone()["status"] == db.STATUS_SKIPPED


def test_build_country_is_bg():
    conn = _conn()
    _seed_parsed(conn, "LZ-Z_2020-01-01", narrative="N" * 200, event_class="Accident")
    pipeline.build(conn)
    assert conn.execute(
        "SELECT country FROM aaiubg_accidents WHERE case_id='LZ-Z_2020-01-01'"
    ).fetchone()["country"] == "BG"


def test_build_report_type_from_event_class():
    conn = _conn()
    _seed_parsed(conn, "D-ASXP_2018-08-12", narrative="N" * 200, event_class="Serious incident")
    pipeline.build(conn)
    assert conn.execute(
        "SELECT report_type FROM aaiubg_accidents WHERE case_id='D-ASXP_2018-08-12'"
    ).fetchone()["report_type"] == "Serious incident"


def test_build_mixed_rows():
    conn = _conn()
    narr = "Z" * 200
    _seed_parsed(conn, "A_2024-01-01", narrative=narr, event_class="Accident")
    _seed_parsed(conn, "B_2024-01-02", narrative=narr, event_class="Serious incident")
    _seed_parsed(conn, "C_2024-01-03", narrative="", event_class="Accident")
    assert pipeline.build(conn) == 2
    assert conn.execute("SELECT COUNT(*) FROM aaiubg_accidents").fetchone()[0] == 2
    assert conn.execute(
        "SELECT status FROM aaiubg_reports WHERE case_id='C_2024-01-03'"
    ).fetchone()["status"] == db.STATUS_SKIPPED


def test_build_null_metadata_full_narrative_is_built():
    conn = _conn()
    narr = "N" * 700
    _seed_parsed(conn, "bg-only-narrative", narrative=narr, event_class=None)
    assert pipeline.build(conn) == 1
    acc = conn.execute("SELECT * FROM aaiubg_accidents WHERE case_id='bg-only-narrative'").fetchone()
    assert acc["narrative_text"] == narr
    assert acc["aircraft"] is None
    assert acc["registration"] is None
