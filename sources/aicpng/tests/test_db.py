"""Schema + helper tests for aicpng_ingest.db."""
from aicpng_ingest import db


def test_init_schema_creates_tables(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_schema(conn)
    tabs = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "aicpng_reports" in tabs
    assert "aicpng_accidents" in tabs


def test_accidents_country_default_pg(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_schema(conn)
    conn.execute("INSERT INTO aicpng_accidents (case_id) VALUES ('AIC-26-1003')")
    conn.commit()
    row = conn.execute(
        "SELECT country FROM aicpng_accidents WHERE case_id='AIC-26-1003'").fetchone()
    assert row["country"] == "PG"


def test_accidents_columns_match_ciaiac_shape(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_schema(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(aicpng_accidents)").fetchall()]
    expected = ["case_id", "event_date", "aircraft", "registration", "operator",
                "location", "country", "narrative_text", "probable_cause",
                "source_url", "report_type", "site_slug", "built_at"]
    assert cols == expected


def test_now_ms_is_int():
    assert isinstance(db.now_ms(), int)
