from sub_ingest import db


def test_schema_tables(conn):
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sub_reports", "sub_accidents"} <= tables


def test_accidents_country_and_lang_default(conn):
    conn.execute("INSERT INTO sub_accidents (case_id) VALUES ('motor--2024--x')")
    row = conn.execute("SELECT country, lang FROM sub_accidents").fetchone()
    assert row["country"] == "AT"
    assert row["lang"] == "de"


def test_reports_page_url_unique(conn):
    import sqlite3
    import pytest
    conn.execute("INSERT INTO sub_reports (case_id, page_url) VALUES ('a', 'u1')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO sub_reports (case_id, page_url) VALUES ('b', 'u1')")


def test_reports_case_id_pk(conn):
    import sqlite3
    import pytest
    conn.execute("INSERT INTO sub_reports (case_id, page_url) VALUES ('a', 'u1')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO sub_reports (case_id, page_url) VALUES ('a', 'u2')")


def test_init_idempotent(conn):
    db.init_schema(conn)
