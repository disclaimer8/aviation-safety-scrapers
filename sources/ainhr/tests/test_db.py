# tests/test_db.py
import sqlite3
from ainhr_ingest import db


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_schema_creates_tables(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_schema(conn)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "ainhr_reports" in names
    assert "ainhr_accidents" in names


def test_accidents_columns_match_ciaiac_contract(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_schema(conn)
    cols = _cols(conn, "ainhr_accidents")
    expected = {
        "case_id", "event_date", "aircraft", "registration", "operator",
        "location", "country", "narrative_text", "probable_cause",
        "source_url", "report_type", "site_slug", "built_at",
    }
    assert cols == expected


def test_country_defaults_to_hr(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_schema(conn)
    conn.execute("INSERT INTO ainhr_accidents (case_id) VALUES ('x')")
    conn.commit()
    row = conn.execute("SELECT country FROM ainhr_accidents WHERE case_id='x'").fetchone()
    assert row["country"] == "HR"


def test_init_schema_idempotent(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_schema(conn)
    db.init_schema(conn)  # must not raise
