from india_ingest import db


def test_schema_tables(conn):
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"india_reports", "india_accidents"} <= tables


def test_accidents_country_default(conn):
    conn.execute("INSERT INTO india_accidents (case_id) VALUES ('2022_VT-SLH')")
    row = conn.execute("SELECT country FROM india_accidents").fetchone()
    assert row["country"] == "IN"


def test_init_idempotent(conn):
    db.init_schema(conn)
