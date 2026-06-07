from ttsb_ingest import db


def test_schema_tables(conn):
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"ttsb_reports", "ttsb_accidents"} <= tables


def test_accidents_country_default(conn):
    conn.execute("INSERT INTO ttsb_accidents (case_id) VALUES ('b-86002')")
    row = conn.execute("SELECT country FROM ttsb_accidents").fetchone()
    assert row["country"] == "TW"


def test_reports_detail_id_unique(conn):
    import sqlite3
    import pytest
    conn.execute("INSERT INTO ttsb_reports (case_id, detail_id) "
                 "VALUES ('b-86002', '44273')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO ttsb_reports (case_id, detail_id) "
                     "VALUES ('jj2258', '44273')")


def test_reports_case_id_pk(conn):
    import sqlite3
    import pytest
    conn.execute("INSERT INTO ttsb_reports (case_id, detail_id) "
                 "VALUES ('b-86002', '44273')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO ttsb_reports (case_id, detail_id) "
                     "VALUES ('b-86002', '44274')")


def test_init_idempotent(conn):
    db.init_schema(conn)
