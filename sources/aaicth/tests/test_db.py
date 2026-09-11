# tests/test_db.py
"""Smoke tests for schema creation and basic operations."""
import sqlite3
import tempfile
import os
import pytest

from aaicth_ingest import db


class TestSchema:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = db.connect(self.tmp.name)
        db.init_schema(self.conn)

    def teardown_method(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_aaicth_reports_table_exists(self):
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='aaicth_reports'"
        ).fetchone()
        assert row is not None

    def test_aaicth_accidents_table_exists(self):
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='aaicth_accidents'"
        ).fetchone()
        assert row is not None

    def test_insert_report_row(self):
        self.conn.execute(
            "INSERT INTO aaicth_reports "
            "(case_id, seq_no, year, report_type, pdf_url, lang, event_date, status, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("aaicth-3/2009", "3", 2009, "final",
             "https://motdrive.mot.go.th/index.php/s/TOKEN/download",
             "en", "2009-01-01", db.STATUS_NEW, db.now_ms(), db.now_ms()),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM aaicth_reports WHERE case_id='aaicth-3/2009'"
        ).fetchone()
        assert row["status"] == db.STATUS_NEW
        assert row["year"] == 2009

    def test_superseded_by_field(self):
        # Insert interim row and mark it superseded
        ts = db.now_ms()
        self.conn.execute(
            "INSERT INTO aaicth_reports "
            "(case_id, seq_no, year, report_type, status, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("aaicth-21/2017-interim", "21", 2017, "interim", db.STATUS_NEW, ts, ts),
        )
        self.conn.execute(
            "UPDATE aaicth_reports SET superseded_by=? WHERE case_id=?",
            ("aaicth-21/2017", "aaicth-21/2017-interim"),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT superseded_by FROM aaicth_reports WHERE case_id='aaicth-21/2017-interim'"
        ).fetchone()
        assert row["superseded_by"] == "aaicth-21/2017"
