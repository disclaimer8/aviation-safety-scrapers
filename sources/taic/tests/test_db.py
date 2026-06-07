from taic_ingest import db


def test_schema_tables(conn):
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"taic_reports", "taic_accidents"} <= tables


def test_accidents_country_default(conn):
    conn.execute("INSERT INTO taic_accidents (case_id) VALUES ('AO-2020-001')")
    row = conn.execute("SELECT country FROM taic_accidents").fetchone()
    assert row["country"] == "NZ"


def test_init_idempotent(conn):
    db.init_schema(conn)  # second run must not raise
