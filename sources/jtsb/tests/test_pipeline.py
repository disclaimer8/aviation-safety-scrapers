# tests/test_pipeline.py
"""Pipeline tests for jtsb_ingest: discover / fetch / parse / build."""

import os
import pytest

from jtsb_ingest import db
from jtsb_ingest.pipeline import discover, fetch, parse, build
from jtsb_ingest.pdf import MIN_NARRATIVE

# ── helpers ────────────────────────────────────────────────────────────────────

_SAMPLE_ROWS = [
    {
        "case_id": "AA2024-3-1",
        "report_url": "https://jtsb.mlit.go.jp/eng-air_report/AA2024-3-1-JA1234.pdf",
        "pdf_url": "https://jtsb.mlit.go.jp/eng-air_report/AA2024-3-1-JA1234.pdf",
        "jp_pdf_url": "https://jtsb.mlit.go.jp/aircraft/rep-acci/AA2024-3-1-JA1234.pdf",
        "title": None,
        "report_type": "Accident",
        "category": "BIRD STRIKE",
        "flight_phase": "LANDING",
        "aircraft": "Boeing 737",
        "registration": "JA1234",
        "date_of_occurrence": "2024-03-15",
        "location": "Tokyo",
        "operator": "Test Airlines Co.",
    },
    {
        "case_id": "AI2024-5-2",
        "report_url": "https://jtsb.mlit.go.jp/eng-air_report/AI2024-5-2-JA5678.pdf",
        "pdf_url": "https://jtsb.mlit.go.jp/eng-air_report/AI2024-5-2-JA5678.pdf",
        "jp_pdf_url": "https://jtsb.mlit.go.jp/aircraft/rep-inci/AI2024-5-2-JA5678.pdf",
        "title": None,
        "report_type": "Serious Incident",
        "category": "AIRPROX",
        "flight_phase": "EN ROUTE",
        "aircraft": "Airbus A320",
        "registration": "JA5678",
        "date_of_occurrence": "2024-05-22",
        "location": "Osaka",
        "operator": "Another Air Inc.",
    },
    {
        "case_id": "AA2023-1-3",
        "report_url": None,  # no EN pdf
        "pdf_url": None,
        "jp_pdf_url": "https://jtsb.mlit.go.jp/aircraft/rep-acci/AA2023-1-3-JA9900.pdf",
        "title": None,
        "report_type": "Accident",
        "category": "RUNWAY INCURSION",
        "flight_phase": "TAKEOFF",
        "aircraft": "Cessna 172",
        "registration": "JA9900",
        "date_of_occurrence": "2023-01-10",
        "location": "Sapporo",
        "operator": None,
    },
]


def _make_conn():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


def _make_fake_client(rows):
    """Returns a client whose iter_index is monkeypatched."""
    class _Client:
        pass
    return _Client()


# ── discover ───────────────────────────────────────────────────────────────────

class TestDiscover:
    def test_inserts_all_rows(self, monkeypatch):
        conn = _make_conn()
        monkeypatch.setattr("jtsb_ingest.jtsb.iter_index", lambda client: list(_SAMPLE_ROWS))
        count = discover(conn, object())
        assert count == 3

    def test_metadata_stored_correctly(self, monkeypatch):
        conn = _make_conn()
        monkeypatch.setattr("jtsb_ingest.jtsb.iter_index", lambda client: list(_SAMPLE_ROWS))
        discover(conn, object())
        row = conn.execute(
            "SELECT * FROM jtsb_reports WHERE case_id=?", ("AA2024-3-1",)
        ).fetchone()
        assert row is not None
        assert row["report_url"] == "https://jtsb.mlit.go.jp/eng-air_report/AA2024-3-1-JA1234.pdf"
        assert row["pdf_url"] == row["report_url"]
        assert row["jp_pdf_url"] == "https://jtsb.mlit.go.jp/aircraft/rep-acci/AA2024-3-1-JA1234.pdf"
        assert row["report_type"] == "Accident"
        assert row["category"] == "BIRD STRIKE"
        assert row["flight_phase"] == "LANDING"
        assert row["aircraft"] == "Boeing 737"
        assert row["registration"] == "JA1234"
        assert row["date_of_occurrence"] == "2024-03-15"
        assert row["location"] == "Tokyo"
        assert row["operator"] == "Test Airlines Co."
        assert row["status"] == db.STATUS_NEW

    def test_status_is_new(self, monkeypatch):
        conn = _make_conn()
        monkeypatch.setattr("jtsb_ingest.jtsb.iter_index", lambda client: list(_SAMPLE_ROWS))
        discover(conn, object())
        statuses = [r["status"] for r in conn.execute("SELECT status FROM jtsb_reports").fetchall()]
        assert all(s == db.STATUS_NEW for s in statuses)

    def test_idempotent(self, monkeypatch):
        """Running discover twice inserts each row only once."""
        conn = _make_conn()
        monkeypatch.setattr("jtsb_ingest.jtsb.iter_index", lambda client: list(_SAMPLE_ROWS))
        first = discover(conn, object())
        second = discover(conn, object())
        assert first == 3
        assert second == 0
        total = conn.execute("SELECT COUNT(*) FROM jtsb_reports").fetchone()[0]
        assert total == 3

    def test_full_flag_accepted(self, monkeypatch):
        """full=True must not raise and must still insert rows."""
        conn = _make_conn()
        monkeypatch.setattr("jtsb_ingest.jtsb.iter_index", lambda client: list(_SAMPLE_ROWS))
        count = discover(conn, object(), full=True)
        assert count == 3

    def test_serious_incident_row_stored(self, monkeypatch):
        conn = _make_conn()
        monkeypatch.setattr("jtsb_ingest.jtsb.iter_index", lambda client: list(_SAMPLE_ROWS))
        discover(conn, object())
        row = conn.execute(
            "SELECT * FROM jtsb_reports WHERE case_id=?", ("AI2024-5-2",)
        ).fetchone()
        assert row["report_type"] == "Serious Incident"
        assert row["category"] == "AIRPROX"


# ── fetch ──────────────────────────────────────────────────────────────────────

class TestFetch:
    def _seed(self, conn, rows=None):
        """Insert sample rows at status='new'."""
        rows = rows or _SAMPLE_ROWS
        ts = db.now_ms()
        for r in rows:
            conn.execute(
                "INSERT INTO jtsb_reports "
                "(case_id, report_url, pdf_url, jp_pdf_url, report_type, category, "
                "flight_phase, aircraft, registration, date_of_occurrence, location, "
                "operator, status, discovered_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    r["case_id"], r["report_url"], r["pdf_url"], r["jp_pdf_url"],
                    r["report_type"], r["category"], r["flight_phase"],
                    r["aircraft"], r["registration"], r["date_of_occurrence"],
                    r["location"], r["operator"], db.STATUS_NEW, ts, ts,
                ),
            )
        conn.commit()

    def test_rows_with_pdf_url_become_fetched(self, monkeypatch, tmp_path):
        conn = _make_conn()
        self._seed(conn)

        def fake_download(client, url, dest):
            with open(dest, "wb") as f:
                f.write(b"%PDF-dummy")

        monkeypatch.setattr("jtsb_ingest.jtsb.download", fake_download)
        monkeypatch.setattr("jtsb_ingest.pipeline.time.sleep", lambda _: None)

        fetch(conn, object(), str(tmp_path))

        # Rows with pdf_url should be 'fetched' with a path
        row = conn.execute(
            "SELECT status, pdf_path FROM jtsb_reports WHERE case_id=?", ("AA2024-3-1",)
        ).fetchone()
        assert row["status"] == db.STATUS_FETCHED
        assert row["pdf_path"] is not None
        assert row["pdf_path"].endswith(".pdf")

    def test_row_without_pdf_url_becomes_fetched_pdf_path_none(self, monkeypatch, tmp_path):
        conn = _make_conn()
        self._seed(conn)
        monkeypatch.setattr("jtsb_ingest.jtsb.download", lambda *a: None)
        monkeypatch.setattr("jtsb_ingest.pipeline.time.sleep", lambda _: None)

        fetch(conn, object(), str(tmp_path))

        row = conn.execute(
            "SELECT status, pdf_path FROM jtsb_reports WHERE case_id=?", ("AA2023-1-3",)
        ).fetchone()
        assert row["status"] == db.STATUS_FETCHED
        assert row["pdf_path"] is None

    def test_case_id_with_slash_sanitised_in_filename(self, monkeypatch, tmp_path):
        conn = _make_conn()
        slashy_row = {
            **_SAMPLE_ROWS[0],
            "case_id": "AA2024/3/1",
            "pdf_url": "https://jtsb.mlit.go.jp/eng-air_report/AA2024-3-1-JA1234.pdf",
        }
        self._seed(conn, [slashy_row])

        written = []

        def fake_download(client, url, dest):
            written.append(dest)
            with open(dest, "wb") as f:
                f.write(b"%PDF-dummy")

        monkeypatch.setattr("jtsb_ingest.jtsb.download", fake_download)
        monkeypatch.setattr("jtsb_ingest.pipeline.time.sleep", lambda _: None)

        fetch(conn, object(), str(tmp_path))
        assert written, "download was never called"
        assert "/" not in os.path.basename(written[0])

    def test_download_failure_keeps_status_new(self, monkeypatch, tmp_path):
        conn = _make_conn()
        self._seed(conn, [_SAMPLE_ROWS[0]])  # only first row (has pdf_url)

        def exploding_download(client, url, dest):
            raise RuntimeError("network failure")

        monkeypatch.setattr("jtsb_ingest.jtsb.download", exploding_download)
        monkeypatch.setattr("jtsb_ingest.pipeline.time.sleep", lambda _: None)

        fetch(conn, object(), str(tmp_path))

        row = conn.execute(
            "SELECT status FROM jtsb_reports WHERE case_id=?", ("AA2024-3-1",)
        ).fetchone()
        assert row["status"] == db.STATUS_NEW

    def test_returns_count_of_rows_iterated(self, monkeypatch, tmp_path):
        conn = _make_conn()
        self._seed(conn)
        monkeypatch.setattr("jtsb_ingest.jtsb.download", lambda *a: None)
        monkeypatch.setattr("jtsb_ingest.pipeline.time.sleep", lambda _: None)

        count = fetch(conn, object(), str(tmp_path))
        # 3 rows seeded, but download raises for none — all iterated
        assert count == 3


# ── parse ──────────────────────────────────────────────────────────────────────

class TestParse:
    def _seed_fetched(self, conn, case_id, pdf_path):
        ts = db.now_ms()
        conn.execute(
            "INSERT INTO jtsb_reports "
            "(case_id, status, pdf_path, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?)",
            (case_id, db.STATUS_FETCHED, pdf_path, ts, ts),
        )
        conn.commit()

    def test_long_text_becomes_pdf_tier(self, monkeypatch):
        conn = _make_conn()
        self._seed_fetched(conn, "AA2024-3-1", "/some/path.pdf")
        long_text = "A" * (MIN_NARRATIVE + 100)
        monkeypatch.setattr("jtsb_ingest.pipeline.extract_text", lambda path: long_text)

        parse(conn)

        row = conn.execute(
            "SELECT source_tier, narrative_text, status FROM jtsb_reports WHERE case_id=?",
            ("AA2024-3-1",),
        ).fetchone()
        assert row["source_tier"] == "pdf"
        assert row["narrative_text"] == long_text
        assert row["status"] == db.STATUS_PARSED

    def test_short_text_becomes_scanned_tier(self, monkeypatch):
        conn = _make_conn()
        self._seed_fetched(conn, "AI2024-5-2", "/some/path.pdf")
        short_text = "Short narrative text, definitely less than MIN_NARRATIVE."
        assert len(short_text) < MIN_NARRATIVE
        monkeypatch.setattr("jtsb_ingest.pipeline.extract_text", lambda path: short_text)

        parse(conn)

        row = conn.execute(
            "SELECT source_tier, status FROM jtsb_reports WHERE case_id=?",
            ("AI2024-5-2",),
        ).fetchone()
        assert row["source_tier"] == "scanned"
        assert row["status"] == db.STATUS_PARSED

    def test_empty_text_becomes_none_tier(self, monkeypatch):
        conn = _make_conn()
        self._seed_fetched(conn, "AA2023-1-3", "/some/empty.pdf")
        monkeypatch.setattr("jtsb_ingest.pipeline.extract_text", lambda path: "")

        parse(conn)

        row = conn.execute(
            "SELECT source_tier, narrative_text, status FROM jtsb_reports WHERE case_id=?",
            ("AA2023-1-3",),
        ).fetchone()
        assert row["source_tier"] == "none"
        assert (row["narrative_text"] or "") == ""
        assert row["status"] == db.STATUS_PARSED

    def test_no_pdf_path_becomes_none_tier(self, monkeypatch):
        """A row with pdf_path=None should get tier='none' without calling extract_text."""
        conn = _make_conn()
        self._seed_fetched(conn, "AA2024-3-1", None)  # pdf_path=None

        called = []
        monkeypatch.setattr("jtsb_ingest.pipeline.extract_text", lambda p: called.append(p) or "")

        parse(conn)

        row = conn.execute(
            "SELECT source_tier, status FROM jtsb_reports WHERE case_id=?",
            ("AA2024-3-1",),
        ).fetchone()
        assert row["source_tier"] == "none"
        assert row["status"] == db.STATUS_PARSED

    def test_returns_count(self, monkeypatch):
        conn = _make_conn()
        for r in _SAMPLE_ROWS:
            self._seed_fetched(conn, r["case_id"], "/path.pdf")
        monkeypatch.setattr("jtsb_ingest.pipeline.extract_text", lambda p: "A" * 700)

        count = parse(conn)
        assert count == 3


# ── build ──────────────────────────────────────────────────────────────────────

class TestBuild:
    def _seed_parsed(self, conn, case_id, narrative, source_tier,
                     report_type="Accident", aircraft="Boeing 737",
                     registration="JA1234", operator="Test Air",
                     location="Tokyo", date_of_occurrence="2024-03-15",
                     pdf_url="https://jtsb.mlit.go.jp/eng-air_report/x.pdf",
                     report_url="https://jtsb.mlit.go.jp/eng-air_report/x.pdf"):
        ts = db.now_ms()
        conn.execute(
            "INSERT INTO jtsb_reports "
            "(case_id, report_type, aircraft, registration, operator, location, "
            "date_of_occurrence, narrative_text, source_tier, pdf_url, report_url, "
            "status, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                case_id, report_type, aircraft, registration, operator, location,
                date_of_occurrence, narrative, source_tier, pdf_url, report_url,
                db.STATUS_PARSED, ts, ts,
            ),
        )
        conn.commit()

    def test_pdf_tier_above_floor_is_built(self):
        conn = _make_conn()
        narrative = "X" * 200  # well above _NARRATIVE_FLOOR=80
        self._seed_parsed(conn, "AA2024-3-1", narrative, "pdf")

        count = build(conn)
        assert count == 1

        acc = conn.execute(
            "SELECT * FROM jtsb_accidents WHERE case_id=?", ("AA2024-3-1",)
        ).fetchone()
        assert acc is not None
        assert acc["country"] == "JP"
        assert acc["narrative_text"] == narrative
        assert acc["event_date"] == "2024-03-15"
        assert acc["report_type"] == "Accident"
        assert acc["site_slug"] is not None

        report = conn.execute(
            "SELECT status FROM jtsb_reports WHERE case_id=?", ("AA2024-3-1",)
        ).fetchone()
        assert report["status"] == db.STATUS_BUILT

    def test_scanned_tier_is_skipped(self):
        conn = _make_conn()
        narrative = "X" * 200  # long enough, but scanned
        self._seed_parsed(conn, "AI2024-5-2", narrative, "scanned")

        count = build(conn)
        assert count == 0

        report = conn.execute(
            "SELECT status FROM jtsb_reports WHERE case_id=?", ("AI2024-5-2",)
        ).fetchone()
        assert report["status"] == db.STATUS_SKIPPED

        acc = conn.execute(
            "SELECT * FROM jtsb_accidents WHERE case_id=?", ("AI2024-5-2",)
        ).fetchone()
        assert acc is None

    def test_none_tier_is_skipped(self):
        conn = _make_conn()
        self._seed_parsed(conn, "AA2023-1-3", "", "none")

        count = build(conn)
        assert count == 0

        report = conn.execute(
            "SELECT status FROM jtsb_reports WHERE case_id=?", ("AA2023-1-3",)
        ).fetchone()
        assert report["status"] == db.STATUS_SKIPPED

    def test_pdf_tier_below_floor_is_skipped(self):
        """Even with tier='pdf', narrative < 80 chars → skipped."""
        conn = _make_conn()
        narrative = "Short."  # below _NARRATIVE_FLOOR
        self._seed_parsed(conn, "AA2024-3-1", narrative, "pdf")

        count = build(conn)
        assert count == 0

        report = conn.execute(
            "SELECT status FROM jtsb_reports WHERE case_id=?", ("AA2024-3-1",)
        ).fetchone()
        assert report["status"] == db.STATUS_SKIPPED

    def test_country_is_jp(self):
        conn = _make_conn()
        self._seed_parsed(conn, "AA2024-3-1", "X" * 200, "pdf")
        build(conn)
        acc = conn.execute(
            "SELECT country FROM jtsb_accidents WHERE case_id=?", ("AA2024-3-1",)
        ).fetchone()
        assert acc["country"] == "JP"

    def test_report_type_carried_through(self):
        conn = _make_conn()
        self._seed_parsed(conn, "AI2024-5-2", "X" * 200, "pdf", report_type="Serious Incident")
        build(conn)
        acc = conn.execute(
            "SELECT report_type FROM jtsb_accidents WHERE case_id=?", ("AI2024-5-2",)
        ).fetchone()
        assert acc["report_type"] == "Serious Incident"

    def test_source_url_prefers_pdf_url(self):
        conn = _make_conn()
        pdf = "https://example.com/pdf.pdf"
        rep = "https://example.com/report.pdf"
        self._seed_parsed(conn, "AA2024-3-1", "X" * 200, "pdf", pdf_url=pdf, report_url=rep)
        build(conn)
        acc = conn.execute(
            "SELECT source_url FROM jtsb_accidents WHERE case_id=?", ("AA2024-3-1",)
        ).fetchone()
        assert acc["source_url"] == pdf

    def test_source_url_falls_back_to_report_url(self):
        conn = _make_conn()
        rep = "https://example.com/report.pdf"
        self._seed_parsed(conn, "AA2024-3-1", "X" * 200, "pdf", pdf_url=None, report_url=rep)
        build(conn)
        acc = conn.execute(
            "SELECT source_url FROM jtsb_accidents WHERE case_id=?", ("AA2024-3-1",)
        ).fetchone()
        assert acc["source_url"] == rep

    def test_insert_or_replace_idempotent(self):
        conn = _make_conn()
        narrative = "X" * 200
        self._seed_parsed(conn, "AA2024-3-1", narrative, "pdf")
        build(conn)  # builds

        # Reset to parsed for second run
        conn.execute(
            "UPDATE jtsb_reports SET status=? WHERE case_id=?",
            (db.STATUS_PARSED, "AA2024-3-1"),
        )
        conn.commit()
        build(conn)  # should not raise

        total = conn.execute("SELECT COUNT(*) FROM jtsb_accidents").fetchone()[0]
        assert total == 1

    def test_mixed_rows_only_pdf_built(self):
        conn = _make_conn()
        self._seed_parsed(conn, "AA2024-3-1", "X" * 200, "pdf")
        self._seed_parsed(conn, "AI2024-5-2", "X" * 200, "scanned",
                          registration="JA5678", location="Osaka")
        self._seed_parsed(conn, "AA2023-1-3", "", "none",
                          registration="JA9900", location="Sapporo")

        count = build(conn)
        assert count == 1

        statuses = {
            r["case_id"]: r["status"]
            for r in conn.execute("SELECT case_id, status FROM jtsb_reports").fetchall()
        }
        assert statuses["AA2024-3-1"] == db.STATUS_BUILT
        assert statuses["AI2024-5-2"] == db.STATUS_SKIPPED
        assert statuses["AA2023-1-3"] == db.STATUS_SKIPPED
