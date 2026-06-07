# pkbwl_ingest/db.py
import sqlite3
import time

STATUS_NEW = "new"
STATUS_PARSED = "parsed"
STATUS_BUILT = "built"
STATUS_SKIPPED = "skipped"

SCHEMA = """
CREATE TABLE IF NOT EXISTS pkbwl_reports (
    case_id              TEXT PRIMARY KEY,
    pdf_url              TEXT UNIQUE,
    page_url             TEXT,
    report_type          TEXT,
    lang                 TEXT,
    aircraft             TEXT,
    registration         TEXT,
    operator             TEXT,
    occurrence_class     TEXT,
    injury_level         TEXT,
    investigation_status TEXT,
    date_of_occurrence   TEXT,
    location             TEXT,
    pdf_path             TEXT,
    narrative_text       TEXT,
    source_tier          TEXT,
    status               TEXT NOT NULL DEFAULT 'new',
    discovered_at        INTEGER,
    updated_at           INTEGER
);
CREATE TABLE IF NOT EXISTS pkbwl_accidents (
    case_id        TEXT PRIMARY KEY,
    event_date     TEXT,
    aircraft       TEXT,
    registration   TEXT,
    operator       TEXT,
    location       TEXT,
    country        TEXT DEFAULT 'PL',
    lang           TEXT,
    narrative_text TEXT,
    probable_cause TEXT,
    source_url     TEXT,
    report_type    TEXT,
    site_slug      TEXT,
    built_at       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pkbwl_reports_status ON pkbwl_reports(status);
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
