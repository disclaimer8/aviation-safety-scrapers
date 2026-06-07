from tsib_ingest import db


def test_schema_tables(conn):
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"tsib_reports", "tsib_accidents"} <= tables


def test_accidents_country_default(conn):
    conn.execute("INSERT INTO tsib_accidents (case_id) VALUES ('tib-aai-cas-246')")
    row = conn.execute("SELECT country FROM tsib_accidents").fetchone()
    assert row["country"] == "SG"


def test_reports_pk_is_pdf_url(conn):
    cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(tsib_reports)")}
    assert cols["pdf_url"]["pk"] == 1
    # case_id is nullable until fetch
    assert cols["case_id"]["notnull"] == 0


def test_init_idempotent(conn):
    db.init_schema(conn)
