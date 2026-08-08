# tests/test_db.py
"""Tests for db schema and helpers."""
from ntsbcarol_ingest import db


def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


def test_schema_creates_tables():
    conn = _conn()
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "ntsbcarol_cases" in tables
    assert "ntsbcarol_accidents" in tables


def test_insert_and_read_case():
    conn = _conn()
    ts = db.now_ms()
    conn.execute(
        """INSERT INTO ntsbcarol_cases
           (case_id, ntsb_no, mkey, event_date, status, discovered_at, updated_at)
           VALUES (?,?,?,?,?,?,?)""",
        ("ntsbcarol-dca17ia148", "DCA17IA148", "95528", "2017-07-07", db.STATUS_NEW, ts, ts),
    )
    conn.commit()
    row = conn.execute(
        "SELECT case_id, ntsb_no, status FROM ntsbcarol_cases WHERE case_id=?",
        ("ntsbcarol-dca17ia148",),
    ).fetchone()
    assert row["ntsb_no"] == "DCA17IA148"
    assert row["status"] == db.STATUS_NEW


def test_insert_accident():
    conn = _conn()
    ts = db.now_ms()
    conn.execute(
        """INSERT INTO ntsbcarol_accidents
           (case_id, event_date, aircraft, country, narrative_text, source_url, built_at)
           VALUES (?,?,?,?,?,?,?)""",
        (
            "ntsbcarol-dca95ma001",
            "1994-10-31",
            "ATR 72",
            "US",
            "American Eagle Flight 4184 iced over Roselawn Indiana " + "x" * 200,
            "https://carol.ntsb.gov/event/DCA95MA001",
            ts,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT narrative_text, country, lang FROM ntsbcarol_accidents WHERE case_id=?",
        ("ntsbcarol-dca95ma001",),
    ).fetchone()
    assert row["country"] == "US"
    assert row["lang"] == "en"
    assert len(row["narrative_text"]) > 200
