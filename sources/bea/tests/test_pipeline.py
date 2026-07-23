# tests/test_pipeline.py
"""
Pipeline tests for bea discover → fetch → parse → build.
"""
import os
import sys

from bea_ingest import bea, db, pipeline
from bea_ingest.pdf import MIN_NARRATIVE

# Make scripts/ importable without installing
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))


# ── helpers ──────────────────────────────────────────────────────────────────

def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


_FAKE_EVENTS = [
    {
        "slug": "cessna-208-f-hfdz-2026-05-24",
        "detail_url": "https://bea.aero/en/investigation-reports/notified-events/detail/cessna-208-f-hfdz-2026-05-24/",
        "title": "Accident to the Cessna 208 registered F-HFDZ on 24/05/2026 at Frétoy-le-Château AD",
    },
    {
        "slug": "airbus-a320-f-xxxx-2019-02-03",
        "detail_url": "https://bea.aero/en/investigation-reports/notified-events/detail/airbus-a320-f-xxxx-2019-02-03/",
        "title": "Serious incident to the Airbus A320 registered F-XXXX on 03/02/2019 at Paris",
    },
]


# ── discover ──────────────────────────────────────────────────────────────────

def test_discover_inserts_new_rows(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(bea, "iter_events", lambda client: iter(_FAKE_EVENTS))
    assert pipeline.discover(conn, None) == 2
    rows = conn.execute(
        "SELECT slug, status, event_class, aircraft_type, registration, date_of_occurrence "
        "FROM bea_reports ORDER BY slug"
    ).fetchall()
    assert len(rows) == 2
    # Both rows start as 'new'
    assert all(r["status"] == db.STATUS_NEW for r in rows)
    # Title fields are parsed and stored
    r0 = next(r for r in rows if r["slug"] == "cessna-208-f-hfdz-2026-05-24")
    assert r0["event_class"] == "Accident"
    assert r0["aircraft_type"] == "Cessna 208"
    assert r0["registration"] == "F-HFDZ"
    assert r0["date_of_occurrence"] == "2026-05-24"
    r1 = next(r for r in rows if r["slug"] == "airbus-a320-f-xxxx-2019-02-03")
    assert r1["event_class"] == "Serious incident"
    assert r1["registration"] == "F-XXXX"


def test_discover_skips_existing_rows(monkeypatch):
    conn = _conn()
    # Pre-insert one slug
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO bea_reports (slug, status, discovered_at, updated_at) VALUES (?,?,?,?)",
        (_FAKE_EVENTS[0]["slug"], db.STATUS_NEW, ts, ts),
    )
    conn.commit()
    monkeypatch.setattr(bea, "iter_events", lambda client: iter(_FAKE_EVENTS))
    assert pipeline.discover(conn, None) == 1  # only the second slug is new


def test_discover_full_flag_accepted(monkeypatch):
    """full=True should not raise and should still insert new rows."""
    conn = _conn()
    monkeypatch.setattr(bea, "iter_events", lambda client: iter(_FAKE_EVENTS))
    assert pipeline.discover(conn, None, full=True) == 2


def test_discover_no_early_break(monkeypatch):
    """Even when first event is known, all remaining events are still walked."""
    conn = _conn()
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO bea_reports (slug, status, discovered_at, updated_at) VALUES (?,?,?,?)",
        (_FAKE_EVENTS[0]["slug"], db.STATUS_NEW, ts, ts),
    )
    conn.commit()
    walked = []

    def _fake_iter(client):
        for e in _FAKE_EVENTS:
            walked.append(e["slug"])
            yield e

    monkeypatch.setattr(bea, "iter_events", _fake_iter)
    pipeline.discover(conn, None)
    # Both events must have been visited (no early-break on consecutive-known)
    assert walked == [e["slug"] for e in _FAKE_EVENTS]


# ── fetch ─────────────────────────────────────────────────────────────────────

def _seed_new(conn, slug, detail_url):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO bea_reports (slug, detail_url, status, discovered_at, updated_at) VALUES (?,?,?,?,?)",
        (slug, detail_url, db.STATUS_NEW, ts, ts),
    )
    conn.commit()


def test_fetch_downloads_pdf(monkeypatch, tmp_path):
    conn = _conn()
    slug = _FAKE_EVENTS[0]["slug"]
    _seed_new(conn, slug, _FAKE_EVENTS[0]["detail_url"])

    pdf_url = "https://bea.aero/fileadmin/report.pdf"
    monkeypatch.setattr(bea, "get_detail_pdf_url", lambda client, url: pdf_url)
    monkeypatch.setattr(bea, "download", lambda client, url, dest: open(dest, "wb").write(b"%PDF"))

    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    row = conn.execute("SELECT * FROM bea_reports WHERE slug=?", (slug,)).fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert row["pdf_url"] == pdf_url
    assert row["pdf_path"] is not None
    assert os.path.exists(row["pdf_path"])


def test_fetch_no_pdf_on_detail_page(monkeypatch, tmp_path):
    """get_detail_pdf_url returns None (delegated event) → fetched with pdf_path=None."""
    conn = _conn()
    slug = _FAKE_EVENTS[1]["slug"]
    _seed_new(conn, slug, _FAKE_EVENTS[1]["detail_url"])

    monkeypatch.setattr(bea, "get_detail_pdf_url", lambda client, url: None)
    download_called = []
    monkeypatch.setattr(bea, "download", lambda *a: download_called.append(a))

    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    row = conn.execute("SELECT * FROM bea_reports WHERE slug=?", (slug,)).fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert row["pdf_url"] is None
    assert row["pdf_path"] is None
    assert not download_called  # download must not be called when no PDF URL


def test_fetch_pdf_download_failure_keeps_row_fetched(monkeypatch, tmp_path):
    """PDF download failure must not abort; row still advances to 'fetched'."""
    conn = _conn()
    slug = _FAKE_EVENTS[0]["slug"]
    _seed_new(conn, slug, _FAKE_EVENTS[0]["detail_url"])

    monkeypatch.setattr(bea, "get_detail_pdf_url", lambda client, url: "https://bea.aero/bad.pdf")
    monkeypatch.setattr(bea, "download", lambda client, url, dest: (_ for _ in ()).throw(RuntimeError("timeout")))

    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    row = conn.execute("SELECT * FROM bea_reports WHERE slug=?", (slug,)).fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert row["pdf_path"] is None  # failed download → no path


def test_fetch_detail_error_keeps_row_new(monkeypatch, tmp_path):
    """get_detail_pdf_url raising must leave the row at 'new' for retry."""
    conn = _conn()
    slug = _FAKE_EVENTS[0]["slug"]
    _seed_new(conn, slug, _FAKE_EVENTS[0]["detail_url"])

    monkeypatch.setattr(bea, "get_detail_pdf_url", lambda client, url: (_ for _ in ()).throw(RuntimeError("HTTP 500")))

    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    row = conn.execute("SELECT status FROM bea_reports WHERE slug=?", (slug,)).fetchone()
    assert row["status"] == db.STATUS_NEW


def test_fetch_isolates_per_report_errors(monkeypatch, tmp_path):
    """One failing detail fetch must not abort processing of subsequent rows."""
    conn = _conn()
    for e in _FAKE_EVENTS:
        _seed_new(conn, e["slug"], e["detail_url"])

    fail_slug = _FAKE_EVENTS[0]["slug"]
    ok_slug = _FAKE_EVENTS[1]["slug"]

    def _fake_detail(client, url):
        if fail_slug in url:
            raise RuntimeError("HTTP 500")
        return None  # ok slug has no PDF

    monkeypatch.setattr(bea, "get_detail_pdf_url", _fake_detail)
    monkeypatch.setattr(bea, "download", lambda *a: None)

    result = pipeline.fetch(conn, None, str(tmp_path))
    assert result == 2  # iterated over both rows
    assert conn.execute("SELECT status FROM bea_reports WHERE slug=?", (fail_slug,)).fetchone()["status"] == db.STATUS_NEW
    assert conn.execute("SELECT status FROM bea_reports WHERE slug=?", (ok_slug,)).fetchone()["status"] == db.STATUS_FETCHED


# ── parse ─────────────────────────────────────────────────────────────────────

def _seed_fetched(conn, slug, pdf_path=None):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO bea_reports (slug, status, pdf_path, discovered_at, updated_at) VALUES (?,?,?,?,?)",
        (slug, db.STATUS_FETCHED, pdf_path, ts, ts),
    )
    conn.commit()


def test_parse_pdf_above_threshold(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "good", pdf_path="good.pdf")
    long_text = "X" * MIN_NARRATIVE
    monkeypatch.setattr(pipeline, "extract_text", lambda p: long_text)
    assert pipeline.parse(conn) == 1
    row = conn.execute("SELECT narrative_text, source_tier, status FROM bea_reports WHERE slug='good'").fetchone()
    assert row["source_tier"] == "pdf"
    assert row["narrative_text"] == long_text
    assert row["status"] == db.STATUS_PARSED


def test_parse_pdf_below_threshold_yields_none_tier(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "short", pdf_path="short.pdf")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "tiny")
    assert pipeline.parse(conn) == 1
    row = conn.execute("SELECT narrative_text, source_tier, status FROM bea_reports WHERE slug='short'").fetchone()
    assert row["source_tier"] == "none"
    assert row["narrative_text"] == ""
    assert row["status"] == db.STATUS_PARSED


def test_parse_no_pdf_path(monkeypatch):
    """Row with no PDF path (delegated event) → empty narrative, tier='none'."""
    conn = _conn()
    _seed_fetched(conn, "nopdf", pdf_path=None)
    extract_calls = []
    monkeypatch.setattr(pipeline, "extract_text", lambda p: extract_calls.append(p) or "X" * 1000)
    assert pipeline.parse(conn) == 1
    assert not extract_calls  # extract_text must NOT be called when pdf_path is None
    row = conn.execute("SELECT narrative_text, source_tier, status FROM bea_reports WHERE slug='nopdf'").fetchone()
    assert row["source_tier"] == "none"
    assert row["narrative_text"] == ""


def _seed_fetched_with_meta(conn, slug, pdf_path=None, aircraft_type=None,
                             registration=None, date_of_occurrence=None, location=None):
    """Insert a status='fetched' row with optional pre-existing metadata (title-derived)."""
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO bea_reports "
        "(slug, status, pdf_path, aircraft_type, registration, date_of_occurrence, location, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (slug, db.STATUS_FETCHED, pdf_path, aircraft_type, registration,
         date_of_occurrence, location, ts, ts),
    )
    conn.commit()


# English header: real BEA-style header with double-space terminating the location
# (real PDFs use double-space or "(dept)" after the location name; a bare \n does not
# satisfy the _EN_LOC_RE terminator pattern, which requires \s{2,}, (...), or $).
_ENGLISH_HEADER_NARRATIVE = (
    "SAFETY INVESTIGATION REPORT\n"
    "Accident to the Cessna 208 registered F-TEST on 24 September 2023 at Lyon  \n"
) + "x" * 700


def test_parse_header_enriches_metadata_from_narrative(monkeypatch):
    """
    When a fetched row with null metadata has a long narrative whose header
    matches, parse() must populate aircraft_type, registration,
    date_of_occurrence, and location FROM the header parser.
    """
    conn = _conn()
    _seed_fetched_with_meta(conn, "hdr-english", pdf_path="hdr.pdf")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: _ENGLISH_HEADER_NARRATIVE)
    assert pipeline.parse(conn) == 1
    row = conn.execute(
        "SELECT aircraft_type, registration, date_of_occurrence, location, status "
        "FROM bea_reports WHERE slug='hdr-english'"
    ).fetchone()
    assert row["status"] == db.STATUS_PARSED
    # Header must have populated all four fields
    assert row["registration"] == "F-TEST"
    assert row["date_of_occurrence"] == "2023-09-24"
    assert "Cessna 208" in (row["aircraft_type"] or "")
    assert row["location"] is not None and "Lyon" in row["location"]


def test_parse_header_wins_over_existing_null_meta(monkeypatch):
    """
    Header value should overwrite a NULL stored value (null→header value).
    Same as above but verifying the coalesce direction explicitly.
    """
    conn = _conn()
    # Seeded with null registration and date
    _seed_fetched_with_meta(
        conn, "hdr-null-meta", pdf_path="p.pdf",
        aircraft_type=None, registration=None, date_of_occurrence=None, location=None,
    )
    monkeypatch.setattr(pipeline, "extract_text", lambda p: _ENGLISH_HEADER_NARRATIVE)
    pipeline.parse(conn)
    row = conn.execute(
        "SELECT registration, date_of_occurrence FROM bea_reports WHERE slug='hdr-null-meta'"
    ).fetchone()
    assert row["registration"] == "F-TEST"
    assert row["date_of_occurrence"] == "2023-09-24"


def test_parse_header_coalesces_title_fallback(monkeypatch):
    """
    When the header parser returns None for a field (header doesn't cover it),
    the pre-existing title-derived value must be kept (coalesce: header wins,
    title fallback).  We produce a narrative whose header has no date match so
    date_of_occurrence stays as the title value.
    """
    conn = _conn()
    # Title-derived date stored; narrative header won't parse any date
    # Use a narrative that is long enough but lacks a parseable date in the header window
    no_date_header = "SAFETY INVESTIGATION REPORT\n" + "x" * 700
    _seed_fetched_with_meta(
        conn, "hdr-coalesce", pdf_path="p.pdf",
        aircraft_type="Old Aircraft", registration="F-OLD",
        date_of_occurrence="2022-01-15", location="Paris",
    )
    monkeypatch.setattr(pipeline, "extract_text", lambda p: no_date_header)
    pipeline.parse(conn)
    row = conn.execute(
        "SELECT registration, date_of_occurrence, aircraft_type, location "
        "FROM bea_reports WHERE slug='hdr-coalesce'"
    ).fetchone()
    # Header had no reg/date/aircraft/location → title values preserved
    assert row["date_of_occurrence"] == "2022-01-15"
    assert row["registration"] == "F-OLD"
    assert row["aircraft_type"] == "Old Aircraft"
    assert row["location"] == "Paris"


def test_parse_header_skipped_for_short_narrative(monkeypatch):
    """
    When narrative is below MIN_NARRATIVE threshold (tier='none'), header
    parsing must NOT overwrite existing metadata (no narrative = no header).
    """
    conn = _conn()
    _seed_fetched_with_meta(
        conn, "hdr-short", pdf_path="p.pdf",
        aircraft_type="Old Plane", registration="F-ORIG",
        date_of_occurrence="2021-06-01", location="Nice",
    )
    # Short text that would match a header pattern if we mistakenly parsed it
    short_text = "Accident to the Cessna 208 registered F-WRONG on 1 January 2020 at Nowhere"
    monkeypatch.setattr(pipeline, "extract_text", lambda p: short_text)
    pipeline.parse(conn)
    row = conn.execute(
        "SELECT registration, date_of_occurrence, aircraft_type, location, source_tier "
        "FROM bea_reports WHERE slug='hdr-short'"
    ).fetchone()
    # Metadata must remain unchanged (no header parse on short narrative)
    assert row["source_tier"] == "none"
    assert row["registration"] == "F-ORIG"
    assert row["date_of_occurrence"] == "2021-06-01"
    assert row["aircraft_type"] == "Old Plane"
    assert row["location"] == "Nice"


# ── build ─────────────────────────────────────────────────────────────────────

def _seed_parsed(conn, slug, aircraft_type, registration, location,
                 date, narrative, event_class, operator=None, detail_url=None):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO bea_reports "
        "(slug, aircraft_type, registration, location, date_of_occurrence, "
        "narrative_text, event_class, operator, detail_url, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (slug, aircraft_type, registration, location, date,
         narrative, event_class, operator, detail_url, db.STATUS_PARSED, ts, ts),
    )
    conn.commit()


def test_build_creates_accident_row():
    conn = _conn()
    long_narrative = "N" * 200
    _seed_parsed(
        conn,
        slug="cessna-208-f-hfdz-2026-05-24",
        aircraft_type="Cessna 208",
        registration="F-HFDZ",
        location="Frétoy-le-Château AD",
        date="2026-05-24",
        narrative=long_narrative,
        event_class="Accident",
        operator="Air Franche",
        detail_url="https://bea.aero/en/investigation-reports/notified-events/detail/cessna-208-f-hfdz-2026-05-24/",
    )
    assert pipeline.build(conn) == 1
    acc = conn.execute("SELECT * FROM bea_accidents WHERE case_id='cessna-208-f-hfdz-2026-05-24'").fetchone()
    assert acc is not None
    assert acc["country"] == "FR"
    assert acc["event_date"] == "2026-05-24"
    assert acc["aircraft"] == "Cessna 208"
    assert acc["registration"] == "F-HFDZ"
    assert acc["operator"] == "Air Franche"
    assert acc["report_type"] == "Accident"  # stored in bea_accidents.report_type from event_class
    assert acc["narrative_text"] == long_narrative
    assert acc["probable_cause"] is None
    assert acc["source_url"].startswith("https://bea.aero")
    assert acc["site_slug"].startswith("crash-")
    # staging row advances to 'built'
    assert conn.execute(
        "SELECT status FROM bea_reports WHERE slug='cessna-208-f-hfdz-2026-05-24'"
    ).fetchone()["status"] == db.STATUS_BUILT


def test_build_skips_empty_narrative():
    """Row with narrative_text below _NARRATIVE_FLOOR → skipped."""
    conn = _conn()
    _seed_parsed(
        conn,
        slug="delegated-event",
        aircraft_type="Boeing 737",
        registration="F-ABCD",
        location="Paris",
        date="2024-01-01",
        narrative="",  # empty — delegated investigation with no PDF
        event_class="Accident",
        detail_url="https://bea.aero/en/investigation-reports/notified-events/detail/delegated-event/",
    )
    assert pipeline.build(conn) == 0
    assert conn.execute(
        "SELECT status FROM bea_reports WHERE slug='delegated-event'"
    ).fetchone()["status"] == db.STATUS_SKIPPED
    assert conn.execute("SELECT COUNT(*) FROM bea_accidents").fetchone()[0] == 0


def test_build_null_metadata_but_full_narrative_is_built():
    """
    Row with null registration AND null aircraft_type but a 700-char narrative
    must now be BUILT (not skipped).  The skip condition is narrative-only;
    missing metadata is acceptable and the row is emitted with NULL columns.
    """
    conn = _conn()
    long_narrative = "N" * 700
    _seed_parsed(
        conn,
        slug="no-meta-full-narrative",
        aircraft_type=None,
        registration=None,
        location=None,
        date=None,
        narrative=long_narrative,
        event_class=None,
        detail_url="https://bea.aero/en/investigation-reports/notified-events/detail/no-meta-full-narrative/",
    )
    assert pipeline.build(conn) == 1
    acc = conn.execute(
        "SELECT * FROM bea_accidents WHERE case_id='no-meta-full-narrative'"
    ).fetchone()
    assert acc is not None
    assert acc["narrative_text"] == long_narrative
    assert acc["aircraft"] is None
    assert acc["registration"] is None
    assert conn.execute(
        "SELECT status FROM bea_reports WHERE slug='no-meta-full-narrative'"
    ).fetchone()["status"] == db.STATUS_BUILT


def test_build_short_narrative_floor():
    """Narrative of exactly _NARRATIVE_FLOOR - 1 chars → skipped."""
    conn = _conn()
    _seed_parsed(
        conn,
        slug="floor-event",
        aircraft_type="Airbus A320",
        registration="F-XXXX",
        location="Nice",
        date="2022-03-15",
        narrative="X" * 79,  # one below the 80-char floor
        event_class="Serious incident",
        detail_url="https://bea.aero/en/investigation-reports/notified-events/detail/floor-event/",
    )
    assert pipeline.build(conn) == 0
    assert conn.execute(
        "SELECT status FROM bea_reports WHERE slug='floor-event'"
    ).fetchone()["status"] == db.STATUS_SKIPPED


def test_build_relative_detail_url_prefixed():
    """detail_url that is a bare path gets bea.BASE prepended in source_url."""
    conn = _conn()
    _seed_parsed(
        conn,
        slug="relative-url-event",
        aircraft_type="Cessna 172",
        registration="F-GRRR",
        location="Bordeaux",
        date="2021-07-04",
        narrative="R" * 200,
        event_class="Accident",
        detail_url="/en/investigation-reports/notified-events/detail/relative-url-event/",
    )
    pipeline.build(conn)
    acc = conn.execute("SELECT source_url FROM bea_accidents WHERE case_id='relative-url-event'").fetchone()
    assert acc["source_url"].startswith("https://bea.aero")


def test_build_mixed_rows():
    """Two buildable rows + one skipped → build() returns 2, skipped row correct."""
    conn = _conn()
    long_narr = "Z" * 200
    # buildable row 1
    _seed_parsed(conn, "ev1", "Cessna 208", "F-AA01", "Rennes", "2026-01-01", long_narr, "Accident",
                 detail_url="https://bea.aero/detail/ev1/")
    # buildable row 2
    _seed_parsed(conn, "ev2", "Airbus A320", "F-BB02", "Lyon", "2025-05-20", long_narr, "Serious incident",
                 detail_url="https://bea.aero/detail/ev2/")
    # skipped row (empty narrative)
    _seed_parsed(conn, "ev3", "Boeing 737", "F-CC03", "Paris", "2024-03-10", "", "Incident",
                 detail_url="https://bea.aero/detail/ev3/")
    assert pipeline.build(conn) == 2
    assert conn.execute("SELECT COUNT(*) FROM bea_accidents").fetchone()[0] == 2
    assert conn.execute(
        "SELECT status FROM bea_reports WHERE slug='ev3'"
    ).fetchone()["status"] == db.STATUS_SKIPPED


# ── reparse_rebuild migration self-check ─────────────────────────────────────

def _seed_report(conn, slug, title, status, narrative, detail_url=None):
    """Insert a bea_reports row with given fields (metadata all null initially)."""
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO bea_reports "
        "(slug, title, narrative_text, status, detail_url, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (slug, title, narrative, status, detail_url or f"https://bea.aero/detail/{slug}/", ts, ts),
    )
    conn.commit()


def test_reparse_rebuild_on_tiny_db():
    """
    Seed a tiny DB that mirrors the backfill failure scenario and verify that
    reparse_rebuild() produces the expected end-state without touching the FS.

    Rows seeded:
      A – status=skipped, full narrative, parseable title   → reset+built
      B – status=skipped, full narrative, no-class title    → reset+built (narrative only)
      C – status=built,   full narrative, parseable title   → reset+rebuilt (INSERT OR REPLACE)
      D – status=skipped, empty narrative                   → stays skipped
      E – status=parsed,  full narrative, parseable title   → built normally
    """
    from reparse_rebuild import reparse_rebuild  # imported via sys.path above

    conn = _conn()
    long_narr = "N" * 200

    # A: previously skipped, good title
    _seed_report(conn, "slug-a",
                 "Accident to the Cessna 172 registered F-ABCD on 01/01/2024 at Lyon",
                 db.STATUS_SKIPPED, long_narr)
    # B: previously skipped, title with 2-digit year (previously unparseable)
    _seed_report(conn, "slug-b",
                 "Incident to the Airbus A320 registered F-WXYZ on 15/03/24 at Paris",
                 db.STATUS_SKIPPED, long_narr)
    # C: previously built but we want it rebuilt with fresh metadata
    _seed_report(conn, "slug-c",
                 "Serious incident to the Boeing 737 registered G-XYZW on 20/06/2023 near Bordeaux",
                 db.STATUS_BUILT, long_narr)
    # D: skipped, no narrative — must stay skipped
    _seed_report(conn, "slug-d",
                 "Accident to the Piper PA28 registered F-WXAB on 10/10/2022 at Nice",
                 db.STATUS_SKIPPED, "")
    # E: already parsed (new row) — built by normal build()
    _seed_report(conn, "slug-e",
                 "Accident to the Robin DR400 registered F-GLMN on 05/05/2025 at Cannes",
                 db.STATUS_PARSED, long_narr)

    reparse_rebuild(conn, dry_run=False)

    acc_count = conn.execute("SELECT COUNT(*) FROM bea_accidents").fetchone()[0]
    assert acc_count == 4, f"expected 4 accidents (A+B+C+E), got {acc_count}"

    # D must still be skipped
    assert conn.execute(
        "SELECT status FROM bea_reports WHERE slug='slug-d'"
    ).fetchone()["status"] == db.STATUS_SKIPPED

    # A, B, C, E must be built
    for slug in ("slug-a", "slug-b", "slug-c", "slug-e"):
        status = conn.execute(
            "SELECT status FROM bea_reports WHERE slug=?", (slug,)
        ).fetchone()["status"]
        assert status == db.STATUS_BUILT, f"{slug} status={status!r}, expected 'built'"

    # Metadata was re-parsed on slug-b (2-digit year)
    b_acc = conn.execute("SELECT * FROM bea_accidents WHERE case_id='slug-b'").fetchone()
    assert b_acc is not None
    assert b_acc["event_date"] == "2024-03-15"  # 15/03/24 → 2024-03-15


# ── refetch ───────────────────────────────────────────────────────────────────

def _seed_stub(conn, slug, *, date="2026-06-01", narrative="", status=db.STATUS_SKIPPED,
               last_refetch_at=None):
    """A 'skipped' stub: event known, detail page had no PDF at last check."""
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO bea_reports (slug, detail_url, date_of_occurrence, narrative_text, "
        "status, discovered_at, updated_at, last_refetch_at) VALUES (?,?,?,?,?,?,?,?)",
        (slug, f"https://bea.aero/detail/{slug}/", date, narrative, status, ts, ts,
         last_refetch_at),
    )
    conn.commit()


def test_refetch_requeues_recent_empty_stub():
    conn = _conn()
    _seed_stub(conn, "stub-recent")
    assert pipeline.refetch(conn) == 1
    row = conn.execute("SELECT status, last_refetch_at FROM bea_reports WHERE slug='stub-recent'").fetchone()
    assert row["status"] == db.STATUS_NEW
    assert row["last_refetch_at"] is not None


def test_refetch_skips_old_delegated_events():
    conn = _conn()
    _seed_stub(conn, "stub-ancient", date="2019-01-01")
    assert pipeline.refetch(conn) == 0
    assert conn.execute("SELECT status FROM bea_reports WHERE slug='stub-ancient'").fetchone()["status"] == db.STATUS_SKIPPED


def test_refetch_includes_null_date_stubs():
    conn = _conn()
    _seed_stub(conn, "stub-nodate", date=None)
    assert pipeline.refetch(conn) == 1


def test_refetch_respects_cooldown():
    conn = _conn()
    _seed_stub(conn, "stub-cooling", last_refetch_at=db.now_ms() - 86_400_000)  # checked yesterday
    assert pipeline.refetch(conn) == 0
    # ...but a stub past the cooldown re-enters the rotation
    _seed_stub(conn, "stub-due", last_refetch_at=db.now_ms() - pipeline.REFETCH_COOLDOWN_MS - 1)
    assert pipeline.refetch(conn) == 1


def test_refetch_leaves_non_empty_skipped_alone():
    """Short-but-real narratives were parsed from an actual PDF — not stubs."""
    conn = _conn()
    _seed_stub(conn, "short-narr", narrative="Brief note.")
    assert pipeline.refetch(conn) == 0


def test_refetch_respects_limit_oldest_checked_first():
    conn = _conn()
    _seed_stub(conn, "stub-never")                                    # never checked → first
    _seed_stub(conn, "stub-old", last_refetch_at=db.now_ms() - 2 * pipeline.REFETCH_COOLDOWN_MS)
    assert pipeline.refetch(conn, limit=1) == 1
    assert conn.execute("SELECT status FROM bea_reports WHERE slug='stub-never'").fetchone()["status"] == db.STATUS_NEW
    assert conn.execute("SELECT status FROM bea_reports WHERE slug='stub-old'").fetchone()["status"] == db.STATUS_SKIPPED


def test_refetch_then_fetch_builds_late_published_report(monkeypatch, tmp_path):
    """End-to-end: a stub whose page has since gained a PDF becomes a built row."""
    conn = _conn()
    _seed_stub(conn, "late-report")
    long_narr = "x" * (MIN_NARRATIVE + 10)

    monkeypatch.setattr(bea, "get_detail_pdf_url", lambda client, url: "https://bea.aero/f.pdf")
    monkeypatch.setattr(bea, "download", lambda client, url, path: open(path, "w").write("pdf"))
    monkeypatch.setattr(pipeline, "extract_text", lambda path: long_narr)

    assert pipeline.refetch(conn) == 1
    pipeline.fetch(conn, None, str(tmp_path))
    pipeline.parse(conn)
    assert pipeline.build(conn) == 1
    assert conn.execute("SELECT status FROM bea_reports WHERE slug='late-report'").fetchone()["status"] == db.STATUS_BUILT
    assert conn.execute("SELECT COUNT(*) FROM bea_accidents").fetchone()[0] == 1


def test_refetch_migration_adds_column_to_legacy_db():
    """init_schema must add last_refetch_at to DBs created before the stage."""
    conn = db.connect(":memory:")
    conn.execute(
        "CREATE TABLE bea_reports (slug TEXT PRIMARY KEY, detail_url TEXT, title TEXT, "
        "event_class TEXT, aircraft_type TEXT, registration TEXT, date_of_occurrence TEXT, "
        "location TEXT, operator TEXT, pdf_url TEXT, pdf_path TEXT, narrative_text TEXT, "
        "source_tier TEXT, status TEXT NOT NULL DEFAULT 'new', discovered_at INTEGER, "
        "updated_at INTEGER)"
    )
    conn.commit()
    db.init_schema(conn)  # must not raise; must add the column
    cols = {r[1] for r in conn.execute("PRAGMA table_info(bea_reports)")}
    assert "last_refetch_at" in cols
    db.init_schema(conn)  # idempotent second run


def test_fetch_404_detail_parks_row_back_to_skipped(monkeypatch, tmp_path):
    """Dead detail pages (renamed slugs) must not retry at 'new' every cycle."""
    conn = _conn()
    _seed_stub(conn, "dead-slug")
    assert pipeline.refetch(conn) == 1

    class _Resp:
        status_code = 404

    def _raise_404(client, url):
        err = Exception("404 Not Found")
        err.response = _Resp()
        raise err

    monkeypatch.setattr(bea, "get_detail_pdf_url", _raise_404)
    pipeline.fetch(conn, None, str(tmp_path))
    row = conn.execute(
        "SELECT status, last_refetch_at FROM bea_reports WHERE slug='dead-slug'"
    ).fetchone()
    assert row["status"] == db.STATUS_SKIPPED
    assert row["last_refetch_at"] is not None


def test_fetch_transient_error_keeps_row_new(monkeypatch, tmp_path):
    """Non-404 failures (timeouts, 5xx) still retry at 'new' next cycle."""
    conn = _conn()
    _seed_new(conn, "flaky-slug", "https://bea.aero/detail/flaky-slug/")

    def _raise_timeout(client, url):
        raise Exception("timeout")

    monkeypatch.setattr(bea, "get_detail_pdf_url", _raise_timeout)
    pipeline.fetch(conn, None, str(tmp_path))
    assert conn.execute(
        "SELECT status FROM bea_reports WHERE slug='flaky-slug'"
    ).fetchone()["status"] == db.STATUS_NEW
