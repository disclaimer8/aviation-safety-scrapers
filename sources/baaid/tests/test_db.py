# tests/test_db.py
from baaid_ingest import db


def test_init_schema_creates_tables():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"baaid_reports", "baaid_accidents"} <= names


def test_accidents_columns_identical_to_ciaiac():
    # baaid_accidents must mirror ciaiac_accidents column-for-column.
    conn = db.connect(":memory:")
    db.init_schema(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(baaid_accidents)").fetchall()]
    expected = [
        "case_id", "event_date", "aircraft", "registration", "operator",
        "location", "country", "narrative_text", "probable_cause", "source_url",
        "report_type", "site_slug", "built_at",
    ]
    assert cols == expected


def test_reports_status_index_exists():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    idx = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    assert "idx_baaid_reports_status" in idx


def test_now_ms_int():
    assert isinstance(db.now_ms(), int)
