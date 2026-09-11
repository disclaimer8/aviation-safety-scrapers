"""Pipeline tests for aaiahk discover -> fetch -> parse -> build."""
import os

from aaiahk_ingest import aaiahk, db, pipeline
from aaiahk_ingest.pdf import MIN_NARRATIVE


def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


_FAKE_ROWS = [
    {
        "case_id": "IVR-2025-01",
        "report_url": None,
        "pdf_url_es": None,
        "pdf_url_en": "https://www.tlb.gov.hk/aaia/doc/Investigation_Report_IVR-2025-01.pdf",
        "pdf_url": "https://www.tlb.gov.hk/aaia/doc/Investigation_Report_IVR-2025-01.pdf",
        "event_class": "Accident",
        "aircraft": "Airbus A321-211",
        "registration": None,
        "date_of_occurrence": "2018-06-24",
        "location": "Runway Excursion of Airbus A321-211 at Hong Kong International Airport",
        "title": "Runway Excursion of Airbus A321-211 at Hong Kong International Airport",
        "superseded_codes": [],
    },
    {
        "case_id": "ITR-2026-01",
        "report_url": None,
        "pdf_url_es": None,
        "pdf_url_en": "https://www.tlb.gov.hk/aaia/doc/Interim_Statemet_ITR-2026-01_(Eng).pdf",
        "pdf_url": "https://www.tlb.gov.hk/aaia/doc/Interim_Statemet_ITR-2026-01_(Eng).pdf",
        "event_class": "Accident",
        "aircraft": "Niviuk Artik R Paraglider",
        "registration": None,
        "date_of_occurrence": "2025-02-11",
        "location": "Ngong Ping, Ma On Shan, Hong Kong",
        "title": "Loss of Control - Inflight of Niviuk Artik R Paraglider at Ngong Ping",
        "superseded_codes": [],
    },
    {
        "case_id": "PLR-2026-01",
        "report_url": None,
        "pdf_url_es": None,
        "pdf_url_en": None,
        "pdf_url": None,
        "event_class": "Accident",
        "aircraft": "BGD Base 3 Paraglider",
        "registration": None,
        "date_of_occurrence": "2025-12-09",
        "location": "Shek O, Hong Kong",
        "title": "Loss of Control - Inflight of BGD Base 3 Paraglider at Shek O",
        "superseded_codes": [],
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
    monkeypatch.setattr(aaiahk, "parse_listing", lambda html, year_url="": _FAKE_ROWS)
    monkeypatch.setattr(aaiahk, "DELAY", 0)

    assert pipeline.discover(conn, _FakeClient()) == 3

    rows = conn.execute(
        "SELECT case_id, pdf_url, lang, status, event_class, aircraft, "
        "date_of_occurrence, location FROM aaiahk_reports ORDER BY case_id"
    ).fetchall()
    assert len(rows) == 3
    assert all(r["status"] == db.STATUS_NEW for r in rows)

    r_ivr = next(r for r in rows if r["case_id"] == "IVR-2025-01")
    assert r_ivr["pdf_url"].endswith("Investigation_Report_IVR-2025-01.pdf")
    assert r_ivr["lang"] == "en"
    assert r_ivr["event_class"] == "Accident"
    assert r_ivr["aircraft"] == "Airbus A321-211"
    assert r_ivr["date_of_occurrence"] == "2018-06-24"

    # PLR row has no PDF -> lang None
    r_plr = next(r for r in rows if r["case_id"] == "PLR-2026-01")
    assert r_plr["pdf_url"] is None
    assert r_plr["lang"] is None


def test_discover_idempotent(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(aaiahk, "parse_listing", lambda html, year_url="": _FAKE_ROWS)
    monkeypatch.setattr(aaiahk, "DELAY", 0)
    assert pipeline.discover(conn, _FakeClient()) == 3
    assert pipeline.discover(conn, _FakeClient()) == 0
    assert conn.execute("SELECT COUNT(*) FROM aaiahk_reports").fetchone()[0] == 3


def test_discover_skips_existing_case_id(monkeypatch):
    conn = _conn()
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO aaiahk_reports (case_id, status, discovered_at, updated_at) VALUES (?,?,?,?)",
        ("IVR-2025-01", db.STATUS_NEW, ts, ts),
    )
    conn.commit()
    monkeypatch.setattr(aaiahk, "parse_listing", lambda html, year_url="": _FAKE_ROWS)
    monkeypatch.setattr(aaiahk, "DELAY", 0)
    assert pipeline.discover(conn, _FakeClient()) == 2


def test_discover_full_flag_accepted(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(aaiahk, "parse_listing", lambda html, year_url="": _FAKE_ROWS)
    monkeypatch.setattr(aaiahk, "DELAY", 0)
    assert pipeline.discover(conn, _FakeClient(), full=True) == 3


# ── fetch ─────────────────────────────────────────────────────────────────

def _seed_new(conn, case_id, pdf_url):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO aaiahk_reports (case_id, pdf_url, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, pdf_url, db.STATUS_NEW, ts, ts),
    )
    conn.commit()


def test_fetch_downloads_pdf_and_advances(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "IVR-2025-01", "https://www.tlb.gov.hk/aaia/doc/x.pdf")

    calls = []
    def _fake_dl(client, url, dest):
        calls.append((url, dest))
        open(dest, "wb").write(b"%PDF")
    monkeypatch.setattr(aaiahk, "download", _fake_dl)
    monkeypatch.setattr(aaiahk, "DELAY", 0)

    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    row = conn.execute(
        "SELECT status, pdf_path FROM aaiahk_reports WHERE case_id='IVR-2025-01'"
    ).fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert os.path.exists(row["pdf_path"])
    assert len(calls) == 1


def test_fetch_no_pdf_url_advances_with_null_path(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "PLR-2026-01", None)
    calls = []
    monkeypatch.setattr(aaiahk, "download", lambda *a: calls.append(a))
    monkeypatch.setattr(aaiahk, "DELAY", 0)

    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    assert not calls
    row = conn.execute(
        "SELECT status, pdf_path FROM aaiahk_reports WHERE case_id='PLR-2026-01'"
    ).fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert row["pdf_path"] is None


def test_fetch_download_failure_keeps_row_new(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "IVR-2025-01", "https://www.tlb.gov.hk/aaia/doc/x.pdf")
    monkeypatch.setattr(
        aaiahk, "download",
        lambda c, u, d: (_ for _ in ()).throw(RuntimeError("timeout")),
    )
    monkeypatch.setattr(aaiahk, "DELAY", 0)
    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    assert conn.execute(
        "SELECT status FROM aaiahk_reports WHERE case_id='IVR-2025-01'"
    ).fetchone()["status"] == db.STATUS_NEW


def test_fetch_isolates_per_row_errors(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "IVR-2025-01", "https://www.tlb.gov.hk/aaia/doc/a.pdf")
    _seed_new(conn, "IVR-2025-02", "https://www.tlb.gov.hk/aaia/doc/b.pdf")

    def _sel(c, url, dest):
        if "a.pdf" in url:
            raise RuntimeError("403")
        open(dest, "wb").write(b"%PDF")
    monkeypatch.setattr(aaiahk, "download", _sel)
    monkeypatch.setattr(aaiahk, "DELAY", 0)

    assert pipeline.fetch(conn, None, str(tmp_path)) == 2
    assert conn.execute(
        "SELECT status FROM aaiahk_reports WHERE case_id='IVR-2025-01'"
    ).fetchone()["status"] == db.STATUS_NEW
    assert conn.execute(
        "SELECT status FROM aaiahk_reports WHERE case_id='IVR-2025-02'"
    ).fetchone()["status"] == db.STATUS_FETCHED


# ── parse ─────────────────────────────────────────────────────────────────

def _seed_fetched(conn, case_id, pdf_path=None):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO aaiahk_reports (case_id, status, pdf_path, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, db.STATUS_FETCHED, pdf_path, ts, ts),
    )
    conn.commit()


def test_parse_long_narrative(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "IVR-2025-01", pdf_path="x.pdf")
    long_text = "X" * MIN_NARRATIVE
    monkeypatch.setattr(pipeline, "extract_text", lambda p: long_text)
    assert pipeline.parse(conn) == 1
    row = conn.execute(
        "SELECT narrative_text, source_tier, status FROM aaiahk_reports WHERE case_id='IVR-2025-01'"
    ).fetchone()
    assert row["status"] == db.STATUS_PARSED
    assert row["source_tier"] == "pdf"
    assert row["narrative_text"] == long_text


def test_parse_scanned_tier(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "IVR-2025-01", pdf_path="x.pdf")
    short = "Short scanned text under threshold."
    monkeypatch.setattr(pipeline, "extract_text", lambda p: short)
    assert pipeline.parse(conn) == 1
    row = conn.execute(
        "SELECT source_tier, narrative_text FROM aaiahk_reports WHERE case_id='IVR-2025-01'"
    ).fetchone()
    assert row["source_tier"] == "scanned"
    assert row["narrative_text"] == short


def test_parse_no_pdf_path(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "PLR-2026-01", pdf_path=None)
    calls = []
    monkeypatch.setattr(pipeline, "extract_text", lambda p: calls.append(p) or "X" * 1000)
    assert pipeline.parse(conn) == 1
    assert not calls
    row = conn.execute(
        "SELECT source_tier, narrative_text FROM aaiahk_reports WHERE case_id='PLR-2026-01'"
    ).fetchone()
    assert row["source_tier"] == "none"
    assert row["narrative_text"] == ""


def test_parse_empty_extraction(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "IVR-2025-01", pdf_path="x.pdf")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "")
    pipeline.parse(conn)
    assert conn.execute(
        "SELECT source_tier FROM aaiahk_reports WHERE case_id='IVR-2025-01'"
    ).fetchone()["source_tier"] == "none"


# ── build ─────────────────────────────────────────────────────────────────

def _seed_parsed(conn, case_id, *, aircraft=None, registration=None, location=None,
                 date=None, narrative="", event_class=None, operator=None,
                 pdf_url=None, report_url=None):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO aaiahk_reports "
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
    _seed_parsed(conn, "IVR-2025-01", aircraft="Airbus A321-211", location="HKIA",
                 date="2018-06-24", narrative=narr, event_class="Accident",
                 pdf_url="https://www.tlb.gov.hk/aaia/doc/Investigation_Report_IVR-2025-01.pdf")
    assert pipeline.build(conn) == 1
    acc = conn.execute("SELECT * FROM aaiahk_accidents WHERE case_id='IVR-2025-01'").fetchone()
    assert acc["country"] == "HK"
    assert acc["event_date"] == "2018-06-24"
    assert acc["aircraft"] == "Airbus A321-211"
    assert acc["report_type"] == "Accident"
    assert acc["narrative_text"] == narr
    assert acc["source_url"].endswith("Investigation_Report_IVR-2025-01.pdf")
    assert acc["site_slug"].startswith("crash-")
    assert conn.execute(
        "SELECT status FROM aaiahk_reports WHERE case_id='IVR-2025-01'"
    ).fetchone()["status"] == db.STATUS_BUILT


def test_build_country_is_hk():
    conn = _conn()
    _seed_parsed(conn, "IVR-2025-01", narrative="N" * 200, event_class="Accident")
    pipeline.build(conn)
    assert conn.execute(
        "SELECT country FROM aaiahk_accidents WHERE case_id='IVR-2025-01'"
    ).fetchone()["country"] == "HK"


def test_build_skips_empty_narrative():
    conn = _conn()
    _seed_parsed(conn, "IVR-2025-09", narrative="", event_class="Accident")
    assert pipeline.build(conn) == 0
    assert conn.execute(
        "SELECT status FROM aaiahk_reports WHERE case_id='IVR-2025-09'"
    ).fetchone()["status"] == db.STATUS_SKIPPED
    assert conn.execute("SELECT COUNT(*) FROM aaiahk_accidents").fetchone()[0] == 0


def test_build_skips_below_narrative_floor():
    conn = _conn()
    _seed_parsed(conn, "IVR-2025-10", narrative="X" * 79, event_class="Incident")
    assert pipeline.build(conn) == 0
    assert conn.execute(
        "SELECT status FROM aaiahk_reports WHERE case_id='IVR-2025-10'"
    ).fetchone()["status"] == db.STATUS_SKIPPED


def test_build_source_url_falls_back_to_report_url():
    conn = _conn()
    _seed_parsed(conn, "PLR-2026-01", narrative="N" * 200, event_class="Accident",
                 pdf_url=None, report_url="https://www.tlb.gov.hk/aaia/eng/investigation_reports/index.html")
    pipeline.build(conn)
    assert conn.execute(
        "SELECT source_url FROM aaiahk_accidents WHERE case_id='PLR-2026-01'"
    ).fetchone()["source_url"].endswith("index.html")


def test_build_report_type_from_event_class():
    conn = _conn()
    _seed_parsed(conn, "IVR-2025-02", narrative="N" * 200, event_class="Serious incident")
    pipeline.build(conn)
    assert conn.execute(
        "SELECT report_type FROM aaiahk_accidents WHERE case_id='IVR-2025-02'"
    ).fetchone()["report_type"] == "Serious incident"


def test_build_mixed_rows():
    conn = _conn()
    long_narr = "Z" * 200
    _seed_parsed(conn, "IVR-2025-01", narrative=long_narr, event_class="Accident")
    _seed_parsed(conn, "IVR-2025-02", narrative=long_narr, event_class="Incident")
    _seed_parsed(conn, "IVR-2025-03", narrative="", event_class="Accident")
    assert pipeline.build(conn) == 2
    assert conn.execute("SELECT COUNT(*) FROM aaiahk_accidents").fetchone()[0] == 2
    assert conn.execute(
        "SELECT status FROM aaiahk_reports WHERE case_id='IVR-2025-03'"
    ).fetchone()["status"] == db.STATUS_SKIPPED


# ── dedup: prelim/interim → final ──────────────────────────────────────────


def _seed_built_report(conn, case_id, *, superseded_by=None):
    """Seed a row that has already been built (simulates prior run)."""
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO aaiahk_reports "
        "(case_id, status, superseded_by, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, db.STATUS_BUILT, superseded_by, ts, ts),
    )
    conn.execute(
        "INSERT INTO aaiahk_accidents "
        "(case_id, event_date, aircraft, registration, operator, location, country, "
        "narrative_text, probable_cause, source_url, report_type, site_slug, built_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (case_id, "2025-01-01", "Test Aircraft", None, None, "HKIA",
         "HK", "N" * 200, None, "https://example.com/report.pdf",
         "Accident", f"crash-test-{case_id.lower()}", ts),
    )
    conn.commit()


def test_discover_marks_superseded_itr_row(monkeypatch):
    """discover() sets superseded_by when listing returns IVR with superseded ITR code."""
    conn = _conn()
    # Pre-existing ITR row (from a prior run before final was published)
    _seed_built_report(conn, "ITR-2026-01")
    assert conn.execute("SELECT COUNT(*) FROM aaiahk_accidents").fetchone()[0] == 1

    # Now listing returns IVR-2026-XX as canon for the same occurrence row,
    # with ITR-2026-01 in superseded_codes.
    fake_rows_with_ivr = [
        {
            "case_id": "IVR-2027-01",
            "report_url": None, "pdf_url_es": None,
            "pdf_url_en": "https://www.tlb.gov.hk/aaia/doc/IVR-2027-01.pdf",
            "pdf_url": "https://www.tlb.gov.hk/aaia/doc/IVR-2027-01.pdf",
            "event_class": "Accident", "aircraft": "Test Aircraft",
            "registration": None, "date_of_occurrence": "2025-02-11",
            "location": "HKIA", "title": "Test",
            "superseded_codes": ["ITR-2026-01"],
        },
    ]
    monkeypatch.setattr(aaiahk, "parse_listing", lambda html, year_url="": fake_rows_with_ivr)

    inserted = pipeline.discover(conn, _FakeClient())
    assert inserted == 1  # IVR-2027-01 is new

    # ITR-2026-01 must now have superseded_by set
    sup_row = conn.execute(
        "SELECT superseded_by FROM aaiahk_reports WHERE case_id='ITR-2026-01'"
    ).fetchone()
    assert sup_row is not None
    assert sup_row[0] == "IVR-2027-01"


def test_build_purges_superseded_accident_row():
    """build() removes aaiahk_accidents rows marked superseded_by in aaiahk_reports."""
    conn = _conn()
    # Pre-existing ITR accident row (built before final appeared)
    _seed_built_report(conn, "ITR-2026-01", superseded_by="IVR-2027-01")
    assert conn.execute("SELECT COUNT(*) FROM aaiahk_accidents").fetchone()[0] == 1

    # build() with no new parsed rows should still purge the superseded one
    built = pipeline.build(conn)
    assert built == 0
    assert conn.execute("SELECT COUNT(*) FROM aaiahk_accidents").fetchone()[0] == 0


def test_build_does_not_purge_unsuperseded_itr_row():
    """ITR rows without superseded_by (no final yet) are NOT purged by build()."""
    conn = _conn()
    _seed_built_report(conn, "ITR-2026-01", superseded_by=None)
    assert conn.execute("SELECT COUNT(*) FROM aaiahk_accidents").fetchone()[0] == 1

    pipeline.build(conn)
    # Row must survive — it has no final yet
    assert conn.execute("SELECT COUNT(*) FROM aaiahk_accidents").fetchone()[0] == 1


def test_discover_idempotent_superseded_by(monkeypatch):
    """Running discover() twice does not change superseded_by once set."""
    conn = _conn()
    _seed_built_report(conn, "ITR-2026-01", superseded_by="IVR-2027-01")

    fake_rows = [
        {
            "case_id": "IVR-2027-01",
            "report_url": None, "pdf_url_es": None,
            "pdf_url_en": "https://www.tlb.gov.hk/aaia/doc/IVR-2027-01.pdf",
            "pdf_url": "https://www.tlb.gov.hk/aaia/doc/IVR-2027-01.pdf",
            "event_class": "Accident", "aircraft": "Test", "registration": None,
            "date_of_occurrence": "2025-02-11", "location": "HKIA", "title": "T",
            "superseded_codes": ["ITR-2026-01"],
        },
    ]
    # Seed the IVR row too so discover treats it as already known
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO aaiahk_reports (case_id, status, discovered_at, updated_at) VALUES (?,?,?,?)",
        ("IVR-2027-01", db.STATUS_BUILT, ts, ts),
    )
    conn.commit()

    monkeypatch.setattr(aaiahk, "parse_listing", lambda html, year_url="": fake_rows)
    pipeline.discover(conn, _FakeClient())  # second run
    # superseded_by must still point to the correct IVR
    sup = conn.execute(
        "SELECT superseded_by FROM aaiahk_reports WHERE case_id='ITR-2026-01'"
    ).fetchone()[0]
    assert sup == "IVR-2027-01"
