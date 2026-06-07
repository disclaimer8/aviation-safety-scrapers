from aaiu_ingest import db


def test_schema_tables(conn):
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"aaiu_reports", "aaiu_accidents"} <= tables


def test_accidents_country_default(conn):
    conn.execute("INSERT INTO aaiu_accidents (case_id) VALUES ('2026-004')")
    row = conn.execute("SELECT country FROM aaiu_accidents").fetchone()
    assert row["country"] == "IE"


def test_init_idempotent(conn):
    db.init_schema(conn)
