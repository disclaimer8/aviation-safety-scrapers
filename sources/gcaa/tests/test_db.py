from gcaa_ingest import db


def test_schema_tables(conn):
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"gcaa_reports", "gcaa_accidents"} <= tables


def test_accidents_country_default(conn):
    conn.execute("INSERT INTO gcaa_accidents (case_id) VALUES ('aifn-0007-2013')")
    row = conn.execute("SELECT country FROM gcaa_accidents").fetchone()
    assert row["country"] == "AE"


def test_init_idempotent(conn):
    db.init_schema(conn)
