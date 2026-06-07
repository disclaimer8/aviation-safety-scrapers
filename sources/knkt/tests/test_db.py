from knkt_ingest import db


def test_schema_tables(conn):
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"knkt_reports", "knkt_accidents"} <= tables


def test_accidents_country_default(conn):
    conn.execute("INSERT INTO knkt_accidents (case_id) VALUES ('KNKT.07.01.01.04')")
    row = conn.execute("SELECT country FROM knkt_accidents").fetchone()
    assert row["country"] == "ID"


def test_init_idempotent(conn):
    db.init_schema(conn)
