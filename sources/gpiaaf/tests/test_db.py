# tests/test_db.py
import sqlite3

import pytest

from gpiaaf_ingest import db


def test_schema_tables(conn):
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"gpiaaf_reports", "gpiaaf_accidents"} <= tables


def test_accidents_country_default(conn):
    conn.execute("INSERT INTO gpiaaf_accidents (case_id) VALUES ('08-accid-2017')")
    row = conn.execute("SELECT country FROM gpiaaf_accidents").fetchone()
    assert row["country"] == "PT"


def test_reports_lang_default(conn):
    conn.execute("INSERT INTO gpiaaf_reports (case_id) VALUES ('08-accid-2017')")
    row = conn.execute("SELECT lang FROM gpiaaf_reports").fetchone()
    assert row["lang"] == "pt"


def test_reports_case_id_pk(conn):
    conn.execute("INSERT INTO gpiaaf_reports (case_id) VALUES ('08-accid-2017')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO gpiaaf_reports (case_id) VALUES ('08-accid-2017')")


def test_init_idempotent(conn):
    db.init_schema(conn)
    db.init_schema(conn)
