from aibdk_ingest import db


def test_schema_tables(conn):
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"aibdk_reports", "aibdk_accidents"} <= tables


def test_accidents_country_default(conn):
    conn.execute("INSERT INTO aibdk_accidents (case_id) VALUES ('2023-506')")
    row = conn.execute("SELECT country FROM aibdk_accidents").fetchone()
    assert row["country"] == "DK"


def test_init_idempotent(conn):
    db.init_schema(conn)
