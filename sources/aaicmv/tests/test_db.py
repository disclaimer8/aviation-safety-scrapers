# tests/test_db.py
from aaicmv_ingest import db


def test_schema_tables_and_columns():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "aaicmv_reports" in tables
    assert "aaicmv_accidents" in tables

    cols = [r[1] for r in conn.execute("PRAGMA table_info(aaicmv_accidents)").fetchall()]
    # columns IDENTICAL to ciaiac_accidents
    assert cols == [
        "case_id", "event_date", "aircraft", "registration", "operator",
        "location", "country", "narrative_text", "probable_cause",
        "source_url", "report_type", "site_slug", "built_at",
    ]


def test_country_default_mv():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conn.execute("INSERT INTO aaicmv_accidents (case_id) VALUES ('X')")
    conn.commit()
    r = conn.execute("SELECT country FROM aaicmv_accidents WHERE case_id='X'").fetchone()
    assert r["country"] == "MV"
