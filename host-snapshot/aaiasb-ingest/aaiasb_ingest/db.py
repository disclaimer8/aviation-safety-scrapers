# aaiasb_ingest/db.py
import sqlite3
import time

STATUS_NEW = "new"
STATUS_FETCHED = "fetched"
STATUS_PARSED = "parsed"
STATUS_BUILT = "built"
STATUS_SKIPPED = "skipped"

SCHEMA = """
CREATE TABLE IF NOT EXISTS aaiasb_reports (
    case_id            TEXT PRIMARY KEY,
    report_url         TEXT,
    pdf_url            TEXT,
    pdf_url_en         TEXT,
    pdf_url_el         TEXT,
    pdf_path           TEXT,
    report_type        TEXT,
    aircraft           TEXT,
    registration       TEXT,
    operator           TEXT,
    date_of_occurrence TEXT,
    location           TEXT,
    narrative_text     TEXT,
    source_tier        TEXT,
    lang               TEXT,
    status             TEXT NOT NULL DEFAULT 'new',
    discovered_at      INTEGER,
    updated_at         INTEGER
);
CREATE TABLE IF NOT EXISTS aaiasb_accidents (
    case_id        TEXT PRIMARY KEY,
    event_date     TEXT,
    aircraft       TEXT,
    registration   TEXT,
    operator       TEXT,
    location       TEXT,
    country        TEXT DEFAULT 'GR',
    narrative_text TEXT,
    probable_cause TEXT,
    source_url     TEXT,
    report_type    TEXT,
    site_slug      TEXT,
    lang           TEXT,
    built_at       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_aaiasb_reports_status ON aaiasb_reports(status);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def now_ms():
    return int(time.time() * 1000)
