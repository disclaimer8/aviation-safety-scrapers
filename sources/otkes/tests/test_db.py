# tests/test_db.py
import sqlite3

import pytest

from otkes_ingest import db


def test_schema_tables(conn):
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"otkes_reports", "otkes_accidents"} <= tables


def test_accidents_country_default(conn):
    conn.execute("INSERT INTO otkes_accidents (case_id) VALUES ('l2024-01')")
    row = conn.execute("SELECT country FROM otkes_accidents").fetchone()
    assert row["country"] == "FI"


def test_reports_case_id_pk(conn):
    conn.execute("INSERT INTO otkes_reports (case_id, detail_url) "
                 "VALUES ('l2024-01', 'u1')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO otkes_reports (case_id, detail_url) "
                     "VALUES ('l2024-01', 'u2')")


def test_reports_detail_url_unique(conn):
    conn.execute("INSERT INTO otkes_reports (case_id, detail_url) "
                 "VALUES ('l2024-01', 'u1')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO otkes_reports (case_id, detail_url) "
                     "VALUES ('l2024-02', 'u1')")


def test_init_idempotent(conn):
    db.init_schema(conn)
    db.init_schema(conn)
