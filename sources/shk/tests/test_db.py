from shk_ingest import db


def test_schema_tables(conn):
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"shk_reports", "shk_accidents"} <= tables


def test_accidents_country_default(conn):
    conn.execute("INSERT INTO shk_accidents (case_id) VALUES ('accident-se-aaa')")
    row = conn.execute("SELECT country FROM shk_accidents").fetchone()
    assert row["country"] == "SE"


def test_init_idempotent(conn):
    db.init_schema(conn)
