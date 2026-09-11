# tests/test_db.py
"""Tests for ipiaam_ingest/db.py schema initialisation."""
import sqlite3
import tempfile
import os
import pytest
from ipiaam_ingest import db


def _tmp_db():
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    return path


def test_init_schema_creates_tables():
    path = _tmp_db()
    try:
        conn = db.connect(path)
        db.init_schema(conn)
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert 'ipiaam_reports' in tables
        assert 'ipiaam_accidents' in tables
        conn.close()
    finally:
        os.unlink(path)


def test_init_schema_idempotent():
    path = _tmp_db()
    try:
        conn = db.connect(path)
        db.init_schema(conn)
        db.init_schema(conn)  # must not raise
        conn.close()
    finally:
        os.unlink(path)


def test_reports_insert():
    path = _tmp_db()
    try:
        conn = db.connect(path)
        db.init_schema(conn)
        ts = db.now_ms()
        conn.execute(
            'INSERT INTO ipiaam_reports (case_id, report_ref, pdf_url, lang, status, discovered_at, updated_at) '
            'VALUES (?,?,?,?,?,?,?)',
            ('ipiaam-001-incid-a-2025', '001/INCID-A/IPIAAM/2025',
             'https://www.ipiaam.cv/documento/opendoc/1_en.pdf', 'en', 'new', ts, ts),
        )
        conn.commit()
        row = conn.execute('SELECT * FROM ipiaam_reports WHERE case_id=?',
                           ('ipiaam-001-incid-a-2025',)).fetchone()
        assert row is not None
        assert row['status'] == 'new'
        assert row['lang'] == 'en'
        conn.close()
    finally:
        os.unlink(path)


def test_accidents_country_default():
    path = _tmp_db()
    try:
        conn = db.connect(path)
        db.init_schema(conn)
        conn.execute(
            'INSERT INTO ipiaam_accidents (case_id, built_at) VALUES (?,?)',
            ('ipiaam-test', db.now_ms()),
        )
        conn.commit()
        row = conn.execute('SELECT country FROM ipiaam_accidents WHERE case_id=?',
                           ('ipiaam-test',)).fetchone()
        assert row['country'] == 'CV'
        conn.close()
    finally:
        os.unlink(path)
