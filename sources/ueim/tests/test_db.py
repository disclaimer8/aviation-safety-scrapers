from ueim_ingest import db


def test_schema_tables(conn):
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"ueim_reports", "ueim_accidents"} <= tables


def test_accidents_country_default(conn):
    conn.execute("INSERT INTO ueim_accidents (case_id) VALUES ('tc-cck')")
    row = conn.execute("SELECT country FROM ueim_accidents").fetchone()
    assert row["country"] == "TR"


def test_reports_pdf_url_unique(conn):
    import sqlite3
    import pytest
    conn.execute("INSERT INTO ueim_reports (case_id, pdf_url) "
                 "VALUES ('tc-cck', 'u1')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO ueim_reports (case_id, pdf_url) "
                     "VALUES ('tc-syn', 'u1')")


def test_reports_case_id_pk(conn):
    import sqlite3
    import pytest
    conn.execute("INSERT INTO ueim_reports (case_id, pdf_url) "
                 "VALUES ('tc-cck', 'u1')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO ueim_reports (case_id, pdf_url) "
                     "VALUES ('tc-cck', 'u2')")


def test_init_idempotent(conn):
    db.init_schema(conn)
