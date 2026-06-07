from nsia_ingest import db


def test_schema_tables(conn):
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"nsia_reports", "nsia_accidents"} <= tables


def test_accidents_country_default(conn):
    conn.execute("INSERT INTO nsia_accidents (case_id) VALUES ('2024-02')")
    row = conn.execute("SELECT country FROM nsia_accidents").fetchone()
    assert row["country"] == "NO"


def test_init_idempotent(conn):
    db.init_schema(conn)
