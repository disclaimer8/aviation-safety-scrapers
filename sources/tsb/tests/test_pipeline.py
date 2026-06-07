# tests/test_pipeline.py
"""
Pipeline tests for tsb discover → fetch+parse → build.

All tests run entirely offline using in-memory SQLite and monkeypatched
tsb.iter_index / tsb.fetch_report.
"""
import os

from tsb_ingest import db, tsb
from tsb_ingest import pipeline

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _fixture(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


# ── fake index rows (mirrors what tsb.iter_index returns) ─────────────────────

_FAKE_INDEX_ROWS = [
    {
        "case_id": "A11Q0170",
        "report_url": "https://www.tsb.gc.ca/eng/rapports-reports/aviation/2011/a11q0170/a11q0170.html",
        "event_date": "2011-08-29",
        "occurrence_type": "Risk of collision",
        "operator": "Air Inuit",
        "aircraft": "Bombardier DHC-8-315",
        "location": "Kuujjuaq, Quebec",
        "occurrence_status": "Completed",
        "registration": None,
    },
    {
        "case_id": "A24A0019",
        "report_url": "https://www.tsb.gc.ca/eng/rapports-reports/aviation/2024/a24a0019/a24a0019.html",
        "event_date": "2024-05-02",
        "occurrence_type": "Collision with terrain",
        "operator": "Custom Helicopters Ltd.",
        "aircraft": "Bell 206L-4",
        "location": "Goose Bay, Newfoundland",
        "occurrence_status": "Completed",
        "registration": None,
    },
    {
        "case_id": "A99C0001",
        "report_url": "https://www.tsb.gc.ca/eng/rapports-reports/aviation/1999/a99c0001/a99c0001.html",
        "event_date": "1999-01-15",
        "occurrence_type": "Runway incursion",
        "operator": None,
        "aircraft": None,
        "location": None,
        "occurrence_status": "Completed",
        "registration": None,
    },
]

_SHORT_ROW = _FAKE_INDEX_ROWS[2]   # no aircraft/location — useful for build-skip tests


# ── helpers ───────────────────────────────────────────────────────────────────

def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


def _seed_new(conn, row):
    """Insert a single index row as status='new'."""
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO tsb_reports "
        "(case_id, report_url, title, occurrence_type, aircraft, registration, "
        "date_of_occurrence, location, operator, occurrence_status, "
        "status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            row["case_id"], row["report_url"], row.get("occurrence_type"),
            row.get("occurrence_type"), row.get("aircraft"), row.get("registration"),
            row.get("event_date"), row.get("location"), row.get("operator"),
            row.get("occurrence_status"), db.STATUS_NEW, ts, ts,
        ),
    )
    conn.commit()


def _seed_parsed(conn, case_id, *, report_url="https://example.com/r.html",
                 occurrence_type="Accident", aircraft="DHC-8",
                 registration=None, location="Kuujjuaq",
                 operator="Air Inuit", date="2011-08-29", narrative=""):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO tsb_reports "
        "(case_id, report_url, title, occurrence_type, aircraft, registration, "
        "date_of_occurrence, location, operator, occurrence_status, "
        "narrative_text, source_tier, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            case_id, report_url, occurrence_type, occurrence_type, aircraft,
            registration, date, location, operator, "Completed",
            narrative, "html", db.STATUS_PARSED, ts, ts,
        ),
    )
    conn.commit()


# ── discover ──────────────────────────────────────────────────────────────────

def test_discover_inserts_new_rows(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(tsb, "iter_index", lambda client: list(_FAKE_INDEX_ROWS))
    assert pipeline.discover(conn, None) == 3
    rows = conn.execute(
        "SELECT case_id, status FROM tsb_reports ORDER BY case_id"
    ).fetchall()
    assert len(rows) == 3
    assert all(r["status"] == db.STATUS_NEW for r in rows)
    ids = {r["case_id"] for r in rows}
    assert ids == {"A11Q0170", "A24A0019", "A99C0001"}


def test_discover_stores_all_index_metadata(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(tsb, "iter_index", lambda client: [_FAKE_INDEX_ROWS[0]])
    pipeline.discover(conn, None)
    row = conn.execute(
        "SELECT * FROM tsb_reports WHERE case_id='A11Q0170'"
    ).fetchone()
    assert row["report_url"] == _FAKE_INDEX_ROWS[0]["report_url"]
    assert row["occurrence_type"] == "Risk of collision"
    assert row["aircraft"] == "Bombardier DHC-8-315"
    assert row["location"] == "Kuujjuaq, Quebec"
    assert row["operator"] == "Air Inuit"
    assert row["date_of_occurrence"] == "2011-08-29"
    assert row["occurrence_status"] == "Completed"


def test_discover_skips_existing_rows(monkeypatch):
    conn = _conn()
    # Pre-insert first row
    _seed_new(conn, _FAKE_INDEX_ROWS[0])
    monkeypatch.setattr(tsb, "iter_index", lambda client: list(_FAKE_INDEX_ROWS))
    assert pipeline.discover(conn, None) == 2  # only 2 new rows


def test_discover_idempotent(monkeypatch):
    """Re-running discover on a fully-known DB returns 0 inserts."""
    conn = _conn()
    monkeypatch.setattr(tsb, "iter_index", lambda client: list(_FAKE_INDEX_ROWS))
    assert pipeline.discover(conn, None) == 3
    assert pipeline.discover(conn, None) == 0


def test_discover_full_flag_accepted(monkeypatch):
    """full=True must not raise and must still insert rows."""
    conn = _conn()
    monkeypatch.setattr(tsb, "iter_index", lambda client: list(_FAKE_INDEX_ROWS))
    assert pipeline.discover(conn, None, full=True) == 3


def test_discover_no_early_break(monkeypatch):
    """Even when the first case_id is known, remaining rows are still walked."""
    conn = _conn()
    _seed_new(conn, _FAKE_INDEX_ROWS[0])

    walked = []

    def _fake_iter(client):
        for r in _FAKE_INDEX_ROWS:
            walked.append(r["case_id"])
            yield r

    monkeypatch.setattr(tsb, "iter_index", _fake_iter)
    pipeline.discover(conn, None)
    assert walked == [r["case_id"] for r in _FAKE_INDEX_ROWS]


# ── fetch (fetch+parse combined) ──────────────────────────────────────────────

def test_fetch_parses_narrative_from_html_fixture(monkeypatch):
    """
    fetch() with a real report HTML fixture → row advances to 'parsed' with
    narrative_text populated (>2000 chars for a completed multi-section report).
    """
    conn = _conn()
    _seed_new(conn, _FAKE_INDEX_ROWS[0])  # A11Q0170

    report_html = _fixture("tsb_report_a11q0170.html")
    monkeypatch.setattr(tsb, "fetch_report", lambda client, url: report_html)
    monkeypatch.setattr(tsb, "DELAY", 0)  # no sleep in tests

    result = pipeline.fetch(conn, None)
    assert result == 1

    row = conn.execute(
        "SELECT narrative_text, source_tier, status FROM tsb_reports WHERE case_id='A11Q0170'"
    ).fetchone()
    assert row["status"] == db.STATUS_PARSED
    assert row["source_tier"] == "html"
    assert len(row["narrative_text"]) > 2000


def test_fetch_failure_keeps_row_new(monkeypatch):
    """A failing HTTP call leaves the row at 'new' for retry."""
    conn = _conn()
    _seed_new(conn, _FAKE_INDEX_ROWS[0])

    def _fail(client, url):
        raise RuntimeError("Connection timeout")

    monkeypatch.setattr(tsb, "fetch_report", _fail)
    monkeypatch.setattr(tsb, "DELAY", 0)

    result = pipeline.fetch(conn, None)
    assert result == 1
    row = conn.execute(
        "SELECT status FROM tsb_reports WHERE case_id='A11Q0170'"
    ).fetchone()
    assert row["status"] == db.STATUS_NEW


def test_fetch_isolates_per_row_errors(monkeypatch):
    """One failing fetch must not abort subsequent rows."""
    conn = _conn()
    for r in _FAKE_INDEX_ROWS[:2]:
        _seed_new(conn, r)

    fail_id = _FAKE_INDEX_ROWS[0]["case_id"]   # A11Q0170 → fail
    ok_id   = _FAKE_INDEX_ROWS[1]["case_id"]   # A24A0019 → succeed

    report_html = _fixture("tsb_report_a24a0019.html")

    def _fake_fetch(client, url):
        if fail_id.lower() in url:
            raise RuntimeError("HTTP 503")
        return report_html

    monkeypatch.setattr(tsb, "fetch_report", _fake_fetch)
    monkeypatch.setattr(tsb, "DELAY", 0)

    result = pipeline.fetch(conn, None)
    assert result == 2
    assert conn.execute(
        "SELECT status FROM tsb_reports WHERE case_id=?", (fail_id,)
    ).fetchone()["status"] == db.STATUS_NEW
    assert conn.execute(
        "SELECT status FROM tsb_reports WHERE case_id=?", (ok_id,)
    ).fetchone()["status"] == db.STATUS_PARSED


def test_fetch_sets_source_tier_html(monkeypatch):
    conn = _conn()
    _seed_new(conn, _FAKE_INDEX_ROWS[1])  # A24A0019

    report_html = _fixture("tsb_report_a24a0019.html")
    monkeypatch.setattr(tsb, "fetch_report", lambda client, url: report_html)
    monkeypatch.setattr(tsb, "DELAY", 0)

    pipeline.fetch(conn, None)
    row = conn.execute(
        "SELECT source_tier FROM tsb_reports WHERE case_id='A24A0019'"
    ).fetchone()
    assert row["source_tier"] == "html"


# ── parse (no-op) ─────────────────────────────────────────────────────────────

def test_parse_noop_returns_zero():
    """parse() is a no-op and returns 0 — fetch+parse are combined."""
    conn = _conn()
    assert pipeline.parse(conn) == 0


# ── build ─────────────────────────────────────────────────────────────────────

def test_build_creates_accident_row_country_ca():
    conn = _conn()
    long_narr = "N" * 500
    _seed_parsed(conn, "A11Q0170", narrative=long_narr, date="2011-08-29",
                 occurrence_type="Risk of collision",
                 report_url="https://www.tsb.gc.ca/eng/rapports-reports/aviation/2011/a11q0170/a11q0170.html")
    assert pipeline.build(conn) == 1

    acc = conn.execute("SELECT * FROM tsb_accidents WHERE case_id='A11Q0170'").fetchone()
    assert acc is not None
    assert acc["country"] == "CA"
    assert acc["event_date"] == "2011-08-29"
    assert acc["report_type"] == "Risk of collision"
    assert acc["source_url"] == "https://www.tsb.gc.ca/eng/rapports-reports/aviation/2011/a11q0170/a11q0170.html"
    assert acc["narrative_text"] == long_narr
    assert acc["probable_cause"] is None
    assert acc["site_slug"].startswith("crash-")

    # staging row advances to 'built'
    assert conn.execute(
        "SELECT status FROM tsb_reports WHERE case_id='A11Q0170'"
    ).fetchone()["status"] == db.STATUS_BUILT


def test_build_stores_operator_from_index():
    conn = _conn()
    _seed_parsed(conn, "A24A0019", narrative="X" * 200,
                 operator="Custom Helicopters Ltd.", aircraft="Bell 206L-4",
                 location="Goose Bay", date="2024-05-02")
    pipeline.build(conn)
    acc = conn.execute("SELECT operator FROM tsb_accidents WHERE case_id='A24A0019'").fetchone()
    assert acc["operator"] == "Custom Helicopters Ltd."


def test_build_skips_short_narrative():
    """Narrative shorter than _NARRATIVE_FLOOR → status='skipped', no accident row."""
    conn = _conn()
    _seed_parsed(conn, "A99C0001", narrative="too short")
    assert pipeline.build(conn) == 0
    assert conn.execute(
        "SELECT status FROM tsb_reports WHERE case_id='A99C0001'"
    ).fetchone()["status"] == db.STATUS_SKIPPED
    assert conn.execute("SELECT COUNT(*) FROM tsb_accidents").fetchone()[0] == 0


def test_build_floor_boundary_79():
    """Narrative of exactly 79 chars → skipped (one below floor of 80)."""
    conn = _conn()
    _seed_parsed(conn, "A99C0001", narrative="X" * 79)
    assert pipeline.build(conn) == 0
    assert conn.execute(
        "SELECT status FROM tsb_reports WHERE case_id='A99C0001'"
    ).fetchone()["status"] == db.STATUS_SKIPPED


def test_build_floor_boundary_80():
    """Narrative of exactly 80 chars meets the floor and must be built."""
    conn = _conn()
    _seed_parsed(conn, "A99C0001", narrative="X" * 80)
    assert pipeline.build(conn) == 1
    acc = conn.execute("SELECT country FROM tsb_accidents WHERE case_id='A99C0001'").fetchone()
    assert acc is not None
    assert acc["country"] == "CA"


def test_build_null_metadata_full_narrative_is_built():
    """
    Row with null aircraft/location/date but a long narrative must be built
    (skip condition is narrative-only; missing metadata is acceptable).
    """
    conn = _conn()
    _seed_parsed(conn, "A99C0001", narrative="N" * 500,
                 aircraft=None, location=None, date=None, operator=None)
    assert pipeline.build(conn) == 1
    acc = conn.execute("SELECT aircraft, location FROM tsb_accidents WHERE case_id='A99C0001'").fetchone()
    assert acc is not None
    assert acc["aircraft"] is None
    assert acc["location"] is None


def test_build_mixed_rows():
    """Two buildable rows + one skipped → returns 2."""
    conn = _conn()
    _seed_parsed(conn, "A11Q0170", narrative="N" * 500, aircraft="DHC-8",
                 location="Kuujjuaq", date="2011-08-29")
    _seed_parsed(conn, "A24A0019", narrative="M" * 300, aircraft="Bell 206L-4",
                 location="Goose Bay", date="2024-05-02")
    _seed_parsed(conn, "A99C0001", narrative="")  # too short → skipped
    assert pipeline.build(conn) == 2
    assert conn.execute("SELECT COUNT(*) FROM tsb_accidents").fetchone()[0] == 2
    assert conn.execute(
        "SELECT status FROM tsb_reports WHERE case_id='A99C0001'"
    ).fetchone()["status"] == db.STATUS_SKIPPED


# ── full pipeline integration ─────────────────────────────────────────────────

def test_full_pipeline_discover_fetch_build(monkeypatch):
    """
    End-to-end: discover (3 rows) → fetch (report HTML faked) → build.
    - A11Q0170: long narrative → built
    - A24A0019: long narrative → built
    - A99C0001: short narrative (empty fetch result) → skipped
    """
    conn = _conn()

    # ── discover ─────────────────────────────────────────────────────────────
    monkeypatch.setattr(tsb, "iter_index", lambda client: list(_FAKE_INDEX_ROWS))
    assert pipeline.discover(conn, None) == 3

    # ── fetch ─────────────────────────────────────────────────────────────────
    report_html_a11 = _fixture("tsb_report_a11q0170.html")
    report_html_a24 = _fixture("tsb_report_a24a0019.html")

    def _fake_fetch(client, url):
        if "a11q0170" in url:
            return report_html_a11
        if "a24a0019" in url:
            return report_html_a24
        # A99C0001 → return minimal HTML with no substantive content
        return "<html><body><main></main></body></html>"

    monkeypatch.setattr(tsb, "fetch_report", _fake_fetch)
    monkeypatch.setattr(tsb, "DELAY", 0)

    fetched = pipeline.fetch(conn, None)
    assert fetched == 3

    # Verify A11Q0170 has a real narrative
    row_a11 = conn.execute(
        "SELECT narrative_text, status FROM tsb_reports WHERE case_id='A11Q0170'"
    ).fetchone()
    assert row_a11["status"] == db.STATUS_PARSED
    assert len(row_a11["narrative_text"]) > 2000

    # ── parse is a no-op ─────────────────────────────────────────────────────
    assert pipeline.parse(conn) == 0

    # ── build ─────────────────────────────────────────────────────────────────
    built = pipeline.build(conn)
    assert built == 2  # A11Q0170 + A24A0019 built; A99C0001 skipped

    assert conn.execute("SELECT COUNT(*) FROM tsb_accidents").fetchone()[0] == 2

    # A99C0001 must be skipped (empty / near-empty narrative)
    assert conn.execute(
        "SELECT status FROM tsb_reports WHERE case_id='A99C0001'"
    ).fetchone()["status"] == db.STATUS_SKIPPED

    # Accident rows carry CA country + correct source_url
    for acc in conn.execute("SELECT * FROM tsb_accidents").fetchall():
        assert acc["country"] == "CA"
        assert acc["source_url"].startswith("https://www.tsb.gc.ca")
