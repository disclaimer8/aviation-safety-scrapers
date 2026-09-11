# tests/test_pipeline.py
"""Pipeline tests for aaiib discover -> fetch -> parse -> build."""
import os

from aaiib_ingest import aaiib, db, pipeline
from aaiib_ingest.pdf import MIN_NARRATIVE


def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


_FAKE_ROWS = [
    {
        "case_id": "AAIIB-2023-RP-C1174",
        "pdf_url": "https://www.caap.gov.ph/wp-content/uploads/2025/05/Accident-RP-C1174.pdf",
        "registration": "RP-C1174",
        "date_of_occurrence": None,
        "event_class": "Accident",
        "title": "Accident RP C1174",
    },
    {
        "case_id": "AAIIB-2008-RP-C229",
        "pdf_url": "https://caap.gov.ph/wp-content/uploads/2023/10/RP-C229_accident-02012008.pdf",
        "registration": "RP-C229",
        "date_of_occurrence": "2008-02-01",
        "event_class": "Accident",
        "title": "RP C229 accident 02012008",
    },
]

_YEAR_URL = "https://www.caap.gov.ph/2023-accidents/"


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
        if url == aaiib.INDEX_URL:
            return _FakeResp(self._index_html)
        return _FakeResp(self._year_html)


# ── discover ────────────────────────────────────────────────────────────────

def test_discover_inserts_new_rows(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(aaiib, "iter_year_urls", lambda html: [_YEAR_URL])
    monkeypatch.setattr(aaiib, "parse_listing", lambda html, year="": _FAKE_ROWS)
    monkeypatch.setattr(aaiib, "DELAY", 0)

    assert pipeline.discover(conn, _FakeClient()) == 2

    rows = conn.execute(
        "SELECT case_id, pdf_url, registration, date_of_occurrence, lang, status, "
        "event_class FROM aaiib_reports ORDER BY case_id"
    ).fetchall()
    assert len(rows) == 2
    assert all(r["status"] == db.STATUS_NEW for r in rows)
    assert all(r["lang"] == "en" for r in rows)

    r = next(x for x in rows if x["case_id"] == "AAIIB-2008-RP-C229")
    assert r["registration"] == "RP-C229"
    assert r["date_of_occurrence"] == "2008-02-01"
    assert r["event_class"] == "Accident"
    assert r["pdf_url"].endswith(".pdf")


def test_discover_idempotent(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(aaiib, "iter_year_urls", lambda html: [_YEAR_URL])
    monkeypatch.setattr(aaiib, "parse_listing", lambda html, year="": _FAKE_ROWS)
    monkeypatch.setattr(aaiib, "DELAY", 0)

    assert pipeline.discover(conn, _FakeClient()) == 2
    assert pipeline.discover(conn, _FakeClient()) == 0
    assert conn.execute("SELECT COUNT(*) FROM aaiib_reports").fetchone()[0] == 2


def test_discover_full_flag_accepted(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(aaiib, "iter_year_urls", lambda html: [_YEAR_URL])
    monkeypatch.setattr(aaiib, "parse_listing", lambda html, year="": _FAKE_ROWS)
    monkeypatch.setattr(aaiib, "DELAY", 0)
    assert pipeline.discover(conn, _FakeClient(), full=True) == 2


# ── fetch ─────────────────────────────────────────────────────────────────

def _seed_new(conn, case_id, pdf_url):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO aaiib_reports (case_id, pdf_url, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, pdf_url, db.STATUS_NEW, ts, ts),
    )
    conn.commit()


def test_fetch_downloads_pdf_and_advances(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "AAIIB-2023-RP-C1174", "https://x/a.pdf")

    calls = []
    def _fake_download(client, url, dest):
        calls.append((url, dest))
        open(dest, "wb").write(b"%PDF")
    monkeypatch.setattr(aaiib, "download", _fake_download)
    monkeypatch.setattr(aaiib, "DELAY", 0)

    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    row = conn.execute(
        "SELECT status, pdf_path FROM aaiib_reports WHERE case_id='AAIIB-2023-RP-C1174'"
    ).fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert row["pdf_path"] is not None and os.path.exists(row["pdf_path"])
    assert len(calls) == 1


def test_fetch_download_failure_keeps_row_new(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "AAIIB-2023-RP-C1174", "https://x/a.pdf")
    monkeypatch.setattr(
        aaiib, "download",
        lambda c, u, d: (_ for _ in ()).throw(RuntimeError("empty body")),
    )
    monkeypatch.setattr(aaiib, "DELAY", 0)

    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    assert conn.execute(
        "SELECT status FROM aaiib_reports WHERE case_id='AAIIB-2023-RP-C1174'"
    ).fetchone()["status"] == db.STATUS_NEW


def test_fetch_isolates_per_row_errors(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "AAIIB-2016-RP-C6919", "https://web.caaplocal.ph/a.pdf")
    _seed_new(conn, "AAIIB-2023-RP-C1174", "https://x/b.pdf")

    def _sel(c, u, d):
        if "caaplocal" in u:
            raise RuntimeError("empty body")
        open(d, "wb").write(b"%PDF")
    monkeypatch.setattr(aaiib, "download", _sel)
    monkeypatch.setattr(aaiib, "DELAY", 0)

    assert pipeline.fetch(conn, None, str(tmp_path)) == 2
    assert conn.execute(
        "SELECT status FROM aaiib_reports WHERE case_id='AAIIB-2016-RP-C6919'"
    ).fetchone()["status"] == db.STATUS_NEW
    assert conn.execute(
        "SELECT status FROM aaiib_reports WHERE case_id='AAIIB-2023-RP-C1174'"
    ).fetchone()["status"] == db.STATUS_FETCHED


# ── parse ─────────────────────────────────────────────────────────────────

def _seed_fetched(conn, case_id, pdf_path=None):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO aaiib_reports (case_id, status, pdf_path, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, db.STATUS_FETCHED, pdf_path, ts, ts),
    )
    conn.commit()


def test_parse_long_narrative_tier_pdf(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "AAIIB-2023-RP-C1174", pdf_path="a.pdf")
    long_text = "X" * MIN_NARRATIVE
    monkeypatch.setattr(pipeline, "extract_text", lambda p: long_text)

    assert pipeline.parse(conn) == 1
    row = conn.execute(
        "SELECT narrative_text, source_tier, status FROM aaiib_reports WHERE case_id='AAIIB-2023-RP-C1174'"
    ).fetchone()
    assert row["status"] == db.STATUS_PARSED
    assert row["source_tier"] == "pdf"
    assert row["narrative_text"] == long_text


def test_parse_short_text_tier_scanned(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "AAIIB-2023-RP-C1174", pdf_path="a.pdf")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "Short scanned blurb.")

    assert pipeline.parse(conn) == 1
    row = conn.execute(
        "SELECT source_tier FROM aaiib_reports WHERE case_id='AAIIB-2023-RP-C1174'"
    ).fetchone()
    assert row["source_tier"] == "scanned"


def test_parse_no_pdf_path_tier_none(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "AAIIB-2023-RP-C1174", pdf_path=None)
    calls = []
    monkeypatch.setattr(pipeline, "extract_text", lambda p: calls.append(p) or "X" * 1000)

    assert pipeline.parse(conn) == 1
    assert not calls
    row = conn.execute(
        "SELECT source_tier, narrative_text FROM aaiib_reports WHERE case_id='AAIIB-2023-RP-C1174'"
    ).fetchone()
    assert row["source_tier"] == "none"
    assert row["narrative_text"] == ""


def test_parse_upgrades_metadata_from_header(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "AAIIB-2022-HL7525", pdf_path="a.pdf")
    header = (
        "DATE OF OCCURRENCE: OCTOBER 23, 2022\n"
        "OPERATOR: KOREAN AIR LINES CO., LTD.\n"
        "PLACE OF OCCURRENCE: MACTAN-CEBU INTERNATIONAL AIRPORT, CEBU, PHILIPPINES\n\n"
        "AAIIB-2025-046\n"
    ) + "Body. " * 200
    monkeypatch.setattr(pipeline, "extract_text", lambda p: header)

    pipeline.parse(conn)
    row = conn.execute(
        "SELECT date_of_occurrence, operator, location, aaiib_ref FROM aaiib_reports "
        "WHERE case_id='AAIIB-2022-HL7525'"
    ).fetchone()
    assert row["date_of_occurrence"] == "2022-10-23"
    assert row["operator"] == "KOREAN AIR LINES CO., LTD."
    assert "MACTAN-CEBU" in row["location"]
    assert row["aaiib_ref"] == "AAIIB-2025-046"


def test_parse_does_not_overwrite_existing_date(monkeypatch):
    """Filename-derived date is kept; header date does not clobber it."""
    conn = _conn()
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO aaiib_reports (case_id, status, pdf_path, date_of_occurrence, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        ("AAIIB-2008-RP-C229", db.STATUS_FETCHED, "a.pdf", "2008-02-01", ts, ts),
    )
    conn.commit()
    monkeypatch.setattr(pipeline, "extract_text",
                        lambda p: "DATE OF OCCURRENCE: JANUARY 1, 1999\n" + "x" * 700)
    pipeline.parse(conn)
    row = conn.execute(
        "SELECT date_of_occurrence FROM aaiib_reports WHERE case_id='AAIIB-2008-RP-C229'"
    ).fetchone()
    assert row["date_of_occurrence"] == "2008-02-01"


# ── build ─────────────────────────────────────────────────────────────────

def _seed_parsed(conn, case_id, *, aircraft=None, registration=None, location=None,
                 date=None, narrative="", event_class="Accident", operator=None,
                 pdf_url=None, report_url=None, source_tier="pdf"):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO aaiib_reports "
        "(case_id, aircraft, registration, location, date_of_occurrence, narrative_text, "
        "event_class, operator, pdf_url, report_url, source_tier, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (case_id, aircraft, registration, location, date, narrative, event_class,
         operator, pdf_url, report_url, source_tier, db.STATUS_PARSED, ts, ts),
    )
    conn.commit()


def test_build_creates_accident_row():
    conn = _conn()
    narr = "N" * 2000
    _seed_parsed(conn, "AAIIB-2023-RP-C1174", aircraft="Cessna U206F",
                 registration="RP-C1174", location="Isabela, Philippines",
                 date="2023-01-24", narrative=narr, event_class="Accident",
                 operator="Our Builders Warehouse",
                 pdf_url="https://x/Accident-RP-C1174.pdf")

    assert pipeline.build(conn) == 1
    acc = conn.execute(
        "SELECT * FROM aaiib_accidents WHERE case_id='AAIIB-2023-RP-C1174'"
    ).fetchone()
    assert acc["country"] == "PH"
    assert acc["event_date"] == "2023-01-24"
    assert acc["aircraft"] == "Cessna U206F"
    assert acc["registration"] == "RP-C1174"
    assert acc["operator"] == "Our Builders Warehouse"
    assert acc["report_type"] == "Accident"
    assert acc["narrative_text"] == narr
    assert acc["source_url"] == "https://x/Accident-RP-C1174.pdf"
    assert acc["site_slug"] == "aaiib-2023-rp-c1174"
    assert conn.execute(
        "SELECT status FROM aaiib_reports WHERE case_id='AAIIB-2023-RP-C1174'"
    ).fetchone()["status"] == db.STATUS_BUILT


def test_build_source_url_falls_back_to_report_url():
    conn = _conn()
    _seed_parsed(conn, "AAIIB-2023-RP-C229", narrative="N" * (pipeline._NARRATIVE_FLOOR + 100),
                 pdf_url=None, report_url="https://www.caap.gov.ph/2023-accidents/")
    pipeline.build(conn)
    acc = conn.execute(
        "SELECT source_url FROM aaiib_accidents WHERE case_id='AAIIB-2023-RP-C229'"
    ).fetchone()
    assert acc["source_url"] == "https://www.caap.gov.ph/2023-accidents/"


def test_build_skips_below_narrative_floor():
    conn = _conn()
    _seed_parsed(conn, "AAIIB-2023-RP-C1", narrative="X" * (pipeline._NARRATIVE_FLOOR - 1))
    assert pipeline.build(conn) == 0
    assert conn.execute(
        "SELECT status FROM aaiib_reports WHERE case_id='AAIIB-2023-RP-C1'"
    ).fetchone()["status"] == db.STATUS_SKIPPED
    assert conn.execute("SELECT COUNT(*) FROM aaiib_accidents").fetchone()[0] == 0


def test_build_skips_scanned_tier():
    conn = _conn()
    _seed_parsed(conn, "AAIIB-2010-RP-C231", narrative="X" * 500, source_tier="scanned")
    assert pipeline.build(conn) == 0
    assert conn.execute(
        "SELECT status FROM aaiib_reports WHERE case_id='AAIIB-2010-RP-C231'"
    ).fetchone()["status"] == db.STATUS_SKIPPED


def test_build_country_is_ph():
    conn = _conn()
    _seed_parsed(conn, "AAIIB-2023-RP-C1174", narrative="N" * (pipeline._NARRATIVE_FLOOR + 100))
    pipeline.build(conn)
    assert conn.execute(
        "SELECT country FROM aaiib_accidents WHERE case_id='AAIIB-2023-RP-C1174'"
    ).fetchone()["country"] == "PH"


def test_build_mixed_rows():
    conn = _conn()
    long_narr = "Z" * (pipeline._NARRATIVE_FLOOR + 100)
    _seed_parsed(conn, "AAIIB-2023-RP-C1", narrative=long_narr)
    _seed_parsed(conn, "AAIIB-2023-RP-C2", narrative=long_narr)
    _seed_parsed(conn, "AAIIB-2023-RP-C3", narrative="", source_tier="none")
    assert pipeline.build(conn) == 2
    assert conn.execute("SELECT COUNT(*) FROM aaiib_accidents").fetchone()[0] == 2
    assert conn.execute(
        "SELECT status FROM aaiib_reports WHERE case_id='AAIIB-2023-RP-C3'"
    ).fetchone()["status"] == db.STATUS_SKIPPED
