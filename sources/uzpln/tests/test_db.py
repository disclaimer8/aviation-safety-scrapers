from uzpln_ingest import db


def test_schema_tables(conn):
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"uzpln_reports", "uzpln_accidents"} <= tables


def test_accidents_country_lang_default(conn):
    conn.execute("INSERT INTO uzpln_accidents (case_id) VALUES ('CZ-25-1428')")
    row = conn.execute(
        "SELECT country, lang FROM uzpln_accidents").fetchone()
    assert row["country"] == "CZ"
    assert row["lang"] == "cs"


def test_reports_incident_id_unique(conn):
    import sqlite3
    import pytest
    conn.execute("INSERT INTO uzpln_reports (case_id, incident_id) "
                 "VALUES ('CZ-25-1428', '830')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO uzpln_reports (case_id, incident_id) "
                     "VALUES ('CZ-25-1379', '830')")


def test_reports_case_id_pk(conn):
    import sqlite3
    import pytest
    conn.execute("INSERT INTO uzpln_reports (case_id, incident_id) "
                 "VALUES ('CZ-25-1428', '830')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO uzpln_reports (case_id, incident_id) "
                     "VALUES ('CZ-25-1428', '824')")


def test_init_idempotent(conn):
    db.init_schema(conn)
