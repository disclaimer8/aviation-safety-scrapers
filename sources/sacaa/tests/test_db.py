from sacaa_ingest import db


def test_schema_tables(conn):
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sacaa_reports", "sacaa_accidents"} <= tables


def test_accidents_country_default(conn):
    conn.execute("INSERT INTO sacaa_accidents (case_id) VALUES ('9690')")
    row = conn.execute("SELECT country FROM sacaa_accidents").fetchone()
    assert row["country"] == "ZA"


def test_init_idempotent(conn):
    db.init_schema(conn)
