# tests/test_pipeline.py
"""
Pipeline tests for bfu discover → fetch → parse → build.
"""
import os

from bfu_ingest import bfu, db, pipeline
from bfu_ingest.pdf import MIN_NARRATIVE

# ── German Identifikation header used as a fake narrative ────────────────────
# Must be ≥ MIN_NARRATIVE (600) chars and contain a parseable Identifikation
# block so that header.parse_header() populates all key fields.
_GERMAN_NARRATIVE = (
    "Identifikation\n"
    "Art des Ereignisses: Unfall\n"
    "Datum:\n\n"
    "16.01.2023\n\n"
    "Ort:\n\n"
    "Rendsburg\n\n"
    "Luftfahrzeug:\n\n"
    "Flugzeug\n\n"
    "Hersteller:\n\n"
    "Learjet Corporation\n\n"
    "Muster:\n\n"
    "Learjet 35 A\n\n"
    "Aktenzeichen:\n\n"
    "BFU23-0022-1X\n\n"
    + "x" * 700  # padding to exceed MIN_NARRATIVE
)

# Fake reports as iter_reports would yield them
_FAKE_REPORTS = [
    {
        "pdf_url": "https://www.bfu-web.de/DE/Publikationen/Untersuchungsberichte/2023/Bericht_23-0022-1X_Learjet35A_Rendsburg.pdf?__blob=publicationFile&v=2",
        "filename": "Bericht_23-0022-1X_Learjet35A_Rendsburg",
        "case_id": "BFU23-0022-1X",
        "title": "Unfall mit Learjet 35A in Rendsburg am 16.01.2023",
    },
    {
        "pdf_url": "https://www.bfu-web.de/DE/Publikationen/Untersuchungsberichte/2022/Bericht_22-0055-3X_CessnaCitation_Hamburg.pdf?__blob=publicationFile&v=1",
        "filename": "Bericht_22-0055-3X_CessnaCitation_Hamburg",
        "case_id": "BFU22-0055-3X",
        "title": "Schwere Störung mit Cessna Citation in Hamburg",
    },
]


# ── helpers ───────────────────────────────────────────────────────────────────

def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


# ── discover ──────────────────────────────────────────────────────────────────

def test_discover_inserts_new_rows(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(bfu, "iter_reports", lambda client, **kw: iter(_FAKE_REPORTS))
    assert pipeline.discover(conn, None) == 2
    rows = conn.execute(
        "SELECT case_id, status FROM bfu_reports ORDER BY case_id"
    ).fetchall()
    assert len(rows) == 2
    assert all(r["status"] == db.STATUS_NEW for r in rows)
    ids = {r["case_id"] for r in rows}
    assert ids == {"BFU23-0022-1X", "BFU22-0055-3X"}


def test_discover_skips_existing_rows(monkeypatch):
    conn = _conn()
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO bfu_reports (case_id, status, discovered_at, updated_at) VALUES (?,?,?,?)",
        ("BFU23-0022-1X", db.STATUS_NEW, ts, ts),
    )
    conn.commit()
    monkeypatch.setattr(bfu, "iter_reports", lambda client, **kw: iter(_FAKE_REPORTS))
    assert pipeline.discover(conn, None) == 1  # only second report is new


def test_discover_idempotent(monkeypatch):
    """Re-running discover on a fully-known DB returns 0 inserts."""
    conn = _conn()
    monkeypatch.setattr(bfu, "iter_reports", lambda client, **kw: iter(_FAKE_REPORTS))
    assert pipeline.discover(conn, None) == 2
    assert pipeline.discover(conn, None) == 0


def test_discover_full_flag_accepted(monkeypatch):
    """full=True must not raise and must still insert rows."""
    conn = _conn()
    monkeypatch.setattr(bfu, "iter_reports", lambda client, **kw: iter(_FAKE_REPORTS))
    assert pipeline.discover(conn, None, full=True) == 2


def test_discover_no_early_break(monkeypatch):
    """Even when the first case_id is known, remaining reports are still walked."""
    conn = _conn()
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO bfu_reports (case_id, status, discovered_at, updated_at) VALUES (?,?,?,?)",
        (_FAKE_REPORTS[0]["case_id"], db.STATUS_NEW, ts, ts),
    )
    conn.commit()
    walked = []

    def _fake_iter(client, **kw):
        for r in _FAKE_REPORTS:
            walked.append(r["case_id"])
            yield r

    monkeypatch.setattr(bfu, "iter_reports", _fake_iter)
    pipeline.discover(conn, None)
    assert walked == [r["case_id"] for r in _FAKE_REPORTS]


def test_discover_stores_pdf_url_as_detail_url(monkeypatch):
    """discover() stores the pdf_url in the detail_url column."""
    conn = _conn()
    monkeypatch.setattr(bfu, "iter_reports", lambda client, **kw: iter([_FAKE_REPORTS[0]]))
    pipeline.discover(conn, None)
    row = conn.execute(
        "SELECT detail_url FROM bfu_reports WHERE case_id=?",
        (_FAKE_REPORTS[0]["case_id"],),
    ).fetchone()
    assert row["detail_url"] == _FAKE_REPORTS[0]["pdf_url"]


# ── fetch ─────────────────────────────────────────────────────────────────────

def _seed_new(conn, case_id, detail_url):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO bfu_reports (case_id, detail_url, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, detail_url, db.STATUS_NEW, ts, ts),
    )
    conn.commit()


def test_fetch_downloads_pdf(monkeypatch, tmp_path):
    conn = _conn()
    r = _FAKE_REPORTS[0]
    _seed_new(conn, r["case_id"], r["pdf_url"])
    monkeypatch.setattr(bfu, "download", lambda client, url, dest: open(dest, "wb").write(b"%PDF"))
    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    row = conn.execute("SELECT * FROM bfu_reports WHERE case_id=?", (r["case_id"],)).fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert row["pdf_path"] is not None
    assert os.path.exists(row["pdf_path"])


def test_fetch_download_failure_keeps_row_new(monkeypatch, tmp_path):
    """A failing download leaves the row at 'new' for retry."""
    conn = _conn()
    r = _FAKE_REPORTS[0]
    _seed_new(conn, r["case_id"], r["pdf_url"])
    monkeypatch.setattr(
        bfu, "download",
        lambda client, url, dest: (_ for _ in ()).throw(RuntimeError("timeout")),
    )
    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    row = conn.execute("SELECT status FROM bfu_reports WHERE case_id=?", (r["case_id"],)).fetchone()
    assert row["status"] == db.STATUS_NEW


def test_fetch_isolates_per_row_errors(monkeypatch, tmp_path):
    """One failing download must not abort subsequent rows."""
    conn = _conn()
    for r in _FAKE_REPORTS:
        _seed_new(conn, r["case_id"], r["pdf_url"])

    fail_id = _FAKE_REPORTS[0]["case_id"]
    ok_id   = _FAKE_REPORTS[1]["case_id"]

    call_count = [0]

    def _fake_download(client, url, dest):
        call_count[0] += 1
        if fail_id in dest:
            raise RuntimeError("HTTP 503")
        open(dest, "wb").write(b"%PDF")

    monkeypatch.setattr(bfu, "download", _fake_download)
    result = pipeline.fetch(conn, None, str(tmp_path))
    assert result == 2
    assert conn.execute(
        "SELECT status FROM bfu_reports WHERE case_id=?", (fail_id,)
    ).fetchone()["status"] == db.STATUS_NEW
    assert conn.execute(
        "SELECT status FROM bfu_reports WHERE case_id=?", (ok_id,)
    ).fetchone()["status"] == db.STATUS_FETCHED


def test_fetch_safe_filename_for_case_id(monkeypatch, tmp_path):
    """Slashes and other unsafe chars in case_id are replaced for the filename."""
    conn = _conn()
    weird_id = "BFU/23-0022/1X"
    conn.execute(
        "INSERT INTO bfu_reports (case_id, detail_url, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (weird_id, "https://example.com/report.pdf", db.STATUS_NEW, db.now_ms(), db.now_ms()),
    )
    conn.commit()
    written_paths = []
    monkeypatch.setattr(
        bfu, "download",
        lambda client, url, dest: (written_paths.append(dest), open(dest, "wb").write(b"%PDF")),
    )
    pipeline.fetch(conn, None, str(tmp_path))
    assert written_paths, "download must have been called"
    assert "/" not in os.path.basename(written_paths[0])


# ── parse ─────────────────────────────────────────────────────────────────────

def _seed_fetched(conn, case_id, pdf_path=None):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO bfu_reports (case_id, status, pdf_path, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, db.STATUS_FETCHED, pdf_path, ts, ts),
    )
    conn.commit()


def test_parse_populates_metadata_from_german_header(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "BFU23-0022-1X", pdf_path="fake.pdf")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: _GERMAN_NARRATIVE)
    assert pipeline.parse(conn) == 1
    row = conn.execute(
        "SELECT event_class, aircraft, date_of_occurrence, location, source_tier, status "
        "FROM bfu_reports WHERE case_id='BFU23-0022-1X'"
    ).fetchone()
    assert row["status"] == db.STATUS_PARSED
    assert row["source_tier"] == "pdf"
    assert row["event_class"] == "Accident"
    assert "Learjet" in (row["aircraft"] or "")
    assert row["date_of_occurrence"] == "2023-01-16"
    assert row["location"] == "Rendsburg"


def test_parse_short_narrative_yields_none_tier(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "BFU22-0055-3X", pdf_path="short.pdf")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "zu kurz")
    assert pipeline.parse(conn) == 1
    row = conn.execute(
        "SELECT narrative_text, source_tier, status FROM bfu_reports WHERE case_id='BFU22-0055-3X'"
    ).fetchone()
    assert row["source_tier"] == "none"
    assert row["narrative_text"] == ""
    assert row["status"] == db.STATUS_PARSED


def test_parse_no_pdf_path_yields_empty(monkeypatch):
    """Row with no pdf_path → empty narrative, tier='none', extract_text not called."""
    conn = _conn()
    _seed_fetched(conn, "BFU22-0099-1X", pdf_path=None)
    extract_calls = []
    monkeypatch.setattr(
        pipeline, "extract_text",
        lambda p: (extract_calls.append(p), "x" * 1000)[1],
    )
    assert pipeline.parse(conn) == 1
    assert not extract_calls
    row = conn.execute(
        "SELECT narrative_text, source_tier, status FROM bfu_reports WHERE case_id='BFU22-0099-1X'"
    ).fetchone()
    assert row["source_tier"] == "none"
    assert row["narrative_text"] == ""


# ── build ─────────────────────────────────────────────────────────────────────

def _seed_parsed(conn, case_id, *, aircraft=None, registration=None, location=None,
                 date=None, narrative="", event_class=None, detail_url=None):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO bfu_reports "
        "(case_id, aircraft, registration, location, date_of_occurrence, "
        "narrative_text, event_class, detail_url, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (case_id, aircraft, registration, location, date,
         narrative, event_class, detail_url, db.STATUS_PARSED, ts, ts),
    )
    conn.commit()


def test_build_creates_accident_row_country_DE():
    conn = _conn()
    _seed_parsed(
        conn, "BFU23-0022-1X",
        aircraft="Learjet Corporation Learjet 35 A",
        location="Rendsburg",
        date="2023-01-16",
        narrative=_GERMAN_NARRATIVE,
        event_class="Accident",
        detail_url="https://www.bfu-web.de/DE/Publikationen/Untersuchungsberichte/2023/Bericht.pdf",
    )
    assert pipeline.build(conn) == 1
    # build() re-parses narrative and uses header Aktenzeichen as canonical case_id
    acc = conn.execute("SELECT * FROM bfu_accidents WHERE case_id='BFU23-0022-1X'").fetchone()
    assert acc is not None
    assert acc["country"] == "DE"
    assert acc["event_date"] == "2023-01-16"
    assert acc["report_type"] == "Accident"
    assert acc["operator"] is None  # BFU doesn't publish operator
    assert acc["narrative_text"] == _GERMAN_NARRATIVE
    assert acc["probable_cause"] is None
    assert acc["site_slug"].startswith("crash-")
    # staging row must advance to 'built'
    assert conn.execute(
        "SELECT status FROM bfu_reports WHERE case_id='BFU23-0022-1X'"
    ).fetchone()["status"] == db.STATUS_BUILT


def test_build_case_id_from_header_aktenzeichen():
    """
    When the header can parse the Aktenzeichen, bfu_accidents.case_id must
    equal the header value (even if staging PK differs slightly in casing).
    """
    conn = _conn()
    # Staging PK is lower-cased; narrative header has the canonical uppercase form
    _seed_parsed(
        conn, "bfu23-0022-1x",  # intentionally mis-cased staging PK
        aircraft="Learjet Corporation Learjet 35 A",
        location="Rendsburg",
        date="2023-01-16",
        narrative=_GERMAN_NARRATIVE,
        event_class="Accident",
        detail_url="https://www.bfu-web.de/Bericht.pdf",
    )
    pipeline.build(conn)
    # Header Aktenzeichen is "BFU23-0022-1X" (from the narrative)
    acc = conn.execute(
        "SELECT case_id FROM bfu_accidents WHERE case_id='BFU23-0022-1X'"
    ).fetchone()
    assert acc is not None, "Canonical case_id from header Aktenzeichen expected"


def test_build_skips_short_narrative():
    """Narrative shorter than _NARRATIVE_FLOOR → status='skipped', no accident row."""
    conn = _conn()
    _seed_parsed(
        conn, "BFU22-0055-3X",
        narrative="zu kurz",
        event_class="Incident",
        detail_url="https://www.bfu-web.de/Bericht.pdf",
    )
    assert pipeline.build(conn) == 0
    assert conn.execute(
        "SELECT status FROM bfu_reports WHERE case_id='BFU22-0055-3X'"
    ).fetchone()["status"] == db.STATUS_SKIPPED
    assert conn.execute("SELECT COUNT(*) FROM bfu_accidents").fetchone()[0] == 0


def test_build_floor_boundary():
    """Narrative of exactly 79 chars → skipped (one below floor of 80)."""
    conn = _conn()
    _seed_parsed(conn, "BFU22-0001-1X", narrative="X" * 79, detail_url="https://x.com/r.pdf")
    assert pipeline.build(conn) == 0
    assert conn.execute(
        "SELECT status FROM bfu_reports WHERE case_id='BFU22-0001-1X'"
    ).fetchone()["status"] == db.STATUS_SKIPPED


def test_build_80_chars_is_built():
    """Narrative of exactly 80 chars meets the floor and must be built."""
    conn = _conn()
    _seed_parsed(conn, "BFU22-0002-1X", narrative="X" * 80, detail_url="https://x.com/r.pdf")
    assert pipeline.build(conn) == 1
    acc = conn.execute("SELECT * FROM bfu_accidents WHERE country='DE'").fetchone()
    assert acc is not None


def test_build_mixed_rows():
    """Two buildable rows + one skipped → returns 2."""
    conn = _conn()
    long_narr = _GERMAN_NARRATIVE
    _seed_parsed(conn, "BFU23-0022-1X", narrative=long_narr, event_class="Accident",
                 aircraft="Learjet Corporation Learjet 35 A", location="Rendsburg",
                 date="2023-01-16", detail_url="https://x.com/r1.pdf")
    _seed_parsed(conn, "BFU22-0055-3X", narrative="Z" * 200, event_class="Serious incident",
                 aircraft="Cessna Citation II", location="Hamburg",
                 date="2022-05-10", detail_url="https://x.com/r2.pdf")
    _seed_parsed(conn, "BFU21-0001-1X", narrative="", event_class="Incident",
                 detail_url="https://x.com/r3.pdf")
    assert pipeline.build(conn) == 2
    assert conn.execute("SELECT COUNT(*) FROM bfu_accidents").fetchone()[0] == 2
    assert conn.execute(
        "SELECT status FROM bfu_reports WHERE case_id='BFU21-0001-1X'"
    ).fetchone()["status"] == db.STATUS_SKIPPED


def test_build_null_metadata_full_narrative_is_built():
    """
    Row with null aircraft/location/date but a full narrative must be built
    (skip condition is narrative-only; missing metadata is acceptable).
    """
    conn = _conn()
    long_narr = "N" * 700
    _seed_parsed(conn, "BFU22-0099-1X", narrative=long_narr, detail_url="https://x.com/r.pdf")
    assert pipeline.build(conn) == 1
    acc = conn.execute("SELECT * FROM bfu_accidents WHERE case_id LIKE '%BFU22-0099%' OR case_id='BFU22-0099-1X'").fetchone()
    assert acc is not None
    assert acc["aircraft"] is None


# ── full pipeline integration ─────────────────────────────────────────────────

def test_full_pipeline_discover_fetch_parse_build(monkeypatch, tmp_path):
    """
    End-to-end: discover (2 rows) → fetch (download faked) → parse (narrative from
    German header) → build → 1 accident (second has empty text → skipped).
    """
    conn = _conn()

    # discover
    monkeypatch.setattr(bfu, "iter_reports", lambda client, **kw: iter(_FAKE_REPORTS))
    assert pipeline.discover(conn, None) == 2

    # fetch: first report gets a real narrative, second gets empty bytes
    def _fake_download(client, url, dest):
        if "23-0022" in dest:
            with open(dest, "wb") as f:
                f.write(b"%PDF-1.4 fake")
        else:
            with open(dest, "wb") as f:
                f.write(b"%PDF-1.4 short")

    monkeypatch.setattr(bfu, "download", _fake_download)
    assert pipeline.fetch(conn, None, str(tmp_path)) == 2

    # parse: monkeypatch extract_text to return real narrative for first, short for second
    def _fake_extract(path):
        if "23-0022" in path:
            return _GERMAN_NARRATIVE
        return "zu kurz"

    monkeypatch.setattr(pipeline, "extract_text", _fake_extract)
    assert pipeline.parse(conn) == 2

    # build: first → built, second → skipped
    assert pipeline.build(conn) == 1
    assert conn.execute("SELECT COUNT(*) FROM bfu_accidents").fetchone()[0] == 1
    acc = conn.execute("SELECT * FROM bfu_accidents").fetchone()
    assert acc["country"] == "DE"
    assert acc["report_type"] == "Accident"
    assert conn.execute(
        "SELECT status FROM bfu_reports WHERE case_id=?",
        (_FAKE_REPORTS[1]["case_id"],),
    ).fetchone()["status"] == db.STATUS_SKIPPED
