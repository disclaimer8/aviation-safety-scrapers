from dgaccl_ingest import db


def test_schema_tables(conn):
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"dgaccl_reports", "dgaccl_accidents"} <= tables


def test_accidents_country_default(conn):
    conn.execute("INSERT INTO dgaccl_accidents (case_id) VALUES ('2044-24')")
    row = conn.execute("SELECT country FROM dgaccl_accidents").fetchone()
    assert row["country"] == "CL"


def test_reports_pdf_url_unique(conn):
    import sqlite3
    import pytest
    conn.execute("INSERT INTO dgaccl_reports (case_id, pdf_url) "
                 "VALUES ('2044-24', 'u1')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO dgaccl_reports (case_id, pdf_url) "
                     "VALUES ('2045-24', 'u1')")


def test_reports_case_id_pk(conn):
    import sqlite3
    import pytest
    conn.execute("INSERT INTO dgaccl_reports (case_id, pdf_url) "
                 "VALUES ('2044-24', 'u1')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO dgaccl_reports (case_id, pdf_url) "
                     "VALUES ('2044-24', 'u2')")


def test_init_idempotent(conn):
    db.init_schema(conn)
