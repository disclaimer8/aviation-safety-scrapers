# tests/test_pipeline.py
"""Pipeline integration tests using in-memory DB + stub HTTP client."""
import os
import tempfile
import pytest

from aaicth_ingest import db
from aaicth_ingest.pipeline import discover, build

# Minimal HTML responses per listing page id
# case_id scheme:
#   final:            aaicth-{seq}/{year}
#   interim:          aaicth-{seq}/{year}-interim
#   serious_incident: aaicth-{seq}/{year}-serious_incident
_ACC_HTML = (
    "<html><body><ul>"
    '<li>File No. 03/2009 (F) ATR72-212A, HS-PGL '
    '<a href="https://motdrive.mot.go.th/index.php/s/MjpUSzi848FFwHR">Thai</a> / '
    '<a href="https://motdrive.mot.go.th/index.php/s/JYnJTqoU34mD19Z">English</a>'
    "</li>"
    '<li>File No. 21/2017 (F) Airbus A320-216, HS-ABB '
    '<a href="https://motdrive.mot.go.th/index.php/s/ll0TXClOFWalK3L">Thai</a> / '
    '<a href="https://motdrive.mot.go.th/index.php/s/JKqBGJdHl2cJKke">English</a>'
    "</li>"
    "</ul></body></html>"
)

_INTERIM_HTML = (
    "<html><body><ul>"
    '<li>File No. 1/2017 Airbus A320-216, HS-ABB '
    '<a href="https://motdrive.mot.go.th/index.php/s/b1KeisMA1mYqceM">Thai</a> / '
    '<a href="https://motdrive.mot.go.th/index.php/s/8jyMsq6t6xYaj8H">English</a>'
    "</li>"
    '<li>File No. 21/2017 Airbus A320, HS-TXB '
    '<a href="https://motdrive.mot.go.th/index.php/s/fV7sVtFJhBCyvj6">Thai</a> / '
    '<a href="https://motdrive.mot.go.th/index.php/s/9tgknMwZuWkbA30">English</a>'
    "</li>"
    "</ul></body></html>"
)

_SI_HTML = (
    "<html><body><ul>"
    '<li>File No. 38/2017 (F) Airbus A320-232, HS-PPE '
    '<a href="https://motdrive.mot.go.th/index.php/s/SYl330ZlCIDCH1x">Thai</a> / '
    '<a href="https://motdrive.mot.go.th/index.php/s/xGS66tF9b9i7Lgj">English</a>'
    "</li>"
    "</ul></body></html>"
)

_LISTING_RESPONSES = {
    "https://ops.mot.go.th/aaic.html?dsfm_lang=EN&id=7": _ACC_HTML,
    "https://ops.mot.go.th/aaic.html?dsfm_lang=EN&id=75": _INTERIM_HTML,
    "https://ops.mot.go.th/aaic.html?dsfm_lang=EN&id=76": _SI_HTML,
}


class _FakeResponse:
    def __init__(self, text):
        self.content = text.encode("utf-8")
    def raise_for_status(self):
        pass


class _StubClient:
    def get(self, url, **kwargs):
        if url in _LISTING_RESPONSES:
            return _FakeResponse(_LISTING_RESPONSES[url])
        raise ValueError(f"Unexpected URL: {url}")


class TestDiscover:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = db.connect(self.tmp.name)
        db.init_schema(self.conn)
        self.client = _StubClient()

    def teardown_method(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_discover_inserts_rows(self):
        n = discover(self.conn, self.client)
        # acc=2, interim=2, SI=1 = 5 total (all distinct case_ids with type suffix)
        assert n == 5

    def test_no_duplicate_case_ids(self):
        discover(self.conn, self.client)
        rows = self.conn.execute("SELECT case_id FROM aaicth_reports").fetchall()
        ids = [r["case_id"] for r in rows]
        assert len(ids) == len(set(ids)), "Duplicate case_ids found"

    def test_interim_21_2017_marked_superseded(self):
        discover(self.conn, self.client)
        # aaicth-21/2017-interim should be superseded by aaicth-21/2017
        interim_row = self.conn.execute(
            "SELECT superseded_by FROM aaicth_reports "
            "WHERE case_id='aaicth-21/2017-interim'"
        ).fetchone()
        assert interim_row is not None, "aaicth-21/2017-interim row must exist"
        assert interim_row["superseded_by"] == "aaicth-21/2017", (
            f"Expected superseded_by='aaicth-21/2017', got {interim_row['superseded_by']}"
        )

    def test_final_rows_not_superseded(self):
        discover(self.conn, self.client)
        final_rows = self.conn.execute(
            "SELECT superseded_by FROM aaicth_reports WHERE report_type='final'"
        ).fetchall()
        for row in final_rows:
            assert row["superseded_by"] is None, "Final rows must not be superseded"

    def test_idempotent(self):
        n1 = discover(self.conn, self.client)
        n2 = discover(self.conn, self.client)
        assert n2 == 0, "Second discover must insert 0 rows (idempotent)"

    def test_final_case_id_no_suffix(self):
        discover(self.conn, self.client)
        row = self.conn.execute(
            "SELECT 1 FROM aaicth_reports WHERE case_id='aaicth-21/2017' AND report_type='final'"
        ).fetchone()
        assert row is not None, "Final row must have case_id without suffix"

    def test_interim_case_id_has_suffix(self):
        discover(self.conn, self.client)
        row = self.conn.execute(
            "SELECT 1 FROM aaicth_reports WHERE case_id='aaicth-1/2017-interim'"
        ).fetchone()
        assert row is not None, "Interim row must have case_id with -interim suffix The aircraft sustained substantial damage during the landing roll and the investigation examined approach speed, runway condition and crew coordination before arriving at its conclusions. Weather at the time was reported as scattered cloud with good visibility."


class TestBuild:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = db.connect(self.tmp.name)
        db.init_schema(self.conn)

    def teardown_method(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def _insert_parsed(self, case_id, report_type, narrative, superseded_by=None):
        ts = db.now_ms()
        self.conn.execute(
            "INSERT INTO aaicth_reports "
            "(case_id, seq_no, year, report_type, pdf_url, lang, event_date, "
            "narrative_text, source_tier, superseded_by, status, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (case_id, "3", 2009, report_type,
             "https://motdrive.mot.go.th/index.php/s/TOKEN/download",
             "en", "2009-08-04", narrative, "pdf", superseded_by,
             db.STATUS_PARSED, ts, ts),
        )
        self.conn.commit()

    def test_build_emits_accident_row(self):
        self._insert_parsed(
            "aaicth-3/2009", "final",
            "This is a factual narrative about an ATR72 accident at Samui International Airport that exceeds the minimum length requirement. The aircraft sustained substantial damage during the landing roll and the investigation examined approach speed, runway condition and crew coordination before arriving at its conclusions. Weather at the time was reported as scattered cloud with good visibility."
        )
        n = build(self.conn)
        assert n == 1
        row = self.conn.execute(
            "SELECT * FROM aaicth_accidents WHERE case_id='aaicth-3/2009'"
        ).fetchone()
        assert row is not None
        assert row["country"] == "TH"
        assert row["report_type"] == "final"

    def test_superseded_row_skipped(self):
        self._insert_parsed(
            "aaicth-21/2017-interim", "interim",
            "Interim narrative text for this case that is more than 80 chars long. The aircraft sustained substantial damage during the landing roll and the investigation examined approach speed, runway condition and crew coordination before arriving at its conclusions. Weather at the time was reported as scattered cloud with good visibility.",
            superseded_by="aaicth-21/2017",
        )
        n = build(self.conn)
        assert n == 0
        row = self.conn.execute(
            "SELECT status FROM aaicth_reports WHERE case_id='aaicth-21/2017-interim'"
        ).fetchone()
        assert row["status"] == db.STATUS_SKIPPED

    def test_short_narrative_skipped(self):
        self._insert_parsed("aaicth-5/2015", "final", "Too short.")
        n = build(self.conn)
        assert n == 0
        row = self.conn.execute(
            "SELECT status FROM aaicth_reports WHERE case_id='aaicth-5/2015'"
        ).fetchone()
        assert row["status"] == db.STATUS_SKIPPED

    def test_site_slug_format(self):
        self._insert_parsed(
            "aaicth-3/2009", "final",
            "Full narrative text about the ATR72 accident at Samui airport - more than eighty characters long. The aircraft sustained substantial damage during the landing roll and the investigation examined approach speed, runway condition and crew coordination before arriving at its conclusions. Weather at the time was reported as scattered cloud with good visibility."
        )
        build(self.conn)
        row = self.conn.execute(
            "SELECT site_slug FROM aaicth_accidents WHERE case_id='aaicth-3/2009'"
        ).fetchone()
        assert row["site_slug"] == "aaicth-3-2009"

    def test_lang_stored_correctly(self):
        self._insert_parsed(
            "aaicth-3/2009", "final",
            "Full narrative text about the ATR72 accident at Samui airport - more than eighty characters long. The aircraft sustained substantial damage during the landing roll and the investigation examined approach speed, runway condition and crew coordination before arriving at its conclusions. Weather at the time was reported as scattered cloud with good visibility."
        )
        build(self.conn)
        row = self.conn.execute(
            "SELECT lang FROM aaicth_accidents WHERE case_id='aaicth-3/2009'"
        ).fetchone()
        assert row["lang"] == "en"
