from jst_ingest import db


def test_schema_tables(conn):
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"jst_reports", "jst_accidents"} <= tables


def test_accidents_country_default(conn):
    conn.execute("INSERT INTO jst_accidents (case_id) VALUES ('12016494')")
    row = conn.execute("SELECT country FROM jst_accidents").fetchone()
    assert row["country"] == "AR"


def test_init_idempotent(conn):
    db.init_schema(conn)
