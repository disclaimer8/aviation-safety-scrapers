from aaibmy_ingest import db


def test_schema_tables(conn):
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"aaibmy_reports", "aaibmy_accidents"} <= tables


def test_accidents_country_default(conn):
    conn.execute("INSERT INTO aaibmy_accidents (case_id) VALUES ('a-08-22p')")
    row = conn.execute("SELECT country FROM aaibmy_accidents").fetchone()
    assert row["country"] == "MY"


def test_reports_case_id_unique(conn):
    import sqlite3
    import pytest
    conn.execute("INSERT INTO aaibmy_reports (pdf_url, case_id) "
                 "VALUES ('u1', 'a-08-22p')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO aaibmy_reports (pdf_url, case_id) "
                     "VALUES ('u2', 'a-08-22p')")


def test_init_idempotent(conn):
    db.init_schema(conn)
