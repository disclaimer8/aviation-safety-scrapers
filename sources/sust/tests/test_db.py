from sust_ingest import db


def test_schema_tables(conn):
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sust_reports", "sust_accidents"} <= tables


def test_accidents_country_default(conn):
    conn.execute("INSERT INTO sust_accidents (case_id) VALUES ('3844')")
    row = conn.execute("SELECT country FROM sust_accidents").fetchone()
    assert row["country"] == "CH"


def test_reports_has_lazyload_and_lang_columns(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sust_reports)")}
    assert {"lazyload_url", "lang", "doc_name"} <= cols


def test_init_idempotent(conn):
    db.init_schema(conn)
