from ovv_ingest import db


def test_schema_tables(conn):
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"ovv_reports", "ovv_accidents"} <= tables


def test_accidents_country_default(conn):
    conn.execute("INSERT INTO ovv_accidents (case_id) VALUES ('crash-ph-abc')")
    row = conn.execute("SELECT country FROM ovv_accidents").fetchone()
    assert row["country"] == "NL"


def test_init_idempotent(conn):
    db.init_schema(conn)
