# aaibmy_ingest/db.py
import sqlite3
import time

STATUS_NEW = "new"
STATUS_PARSED = "parsed"
STATUS_BUILT = "built"
STATUS_SKIPPED = "skipped"

SCHEMA = """
CREATE TABLE IF NOT EXISTS aaibmy_reports (
    pdf_url            TEXT PRIMARY KEY,
    case_id            TEXT UNIQUE,
    page_url           TEXT,
    year               TEXT,
    title              TEXT,
    report_kind        TEXT,
    occurrence_type    TEXT,
    aircraft           TEXT,
    registration       TEXT,
    date_of_occurrence TEXT,
    location           TEXT,
    pdf_path           TEXT,
    narrative_text     TEXT,
    source_tier        TEXT,
    status             TEXT NOT NULL DEFAULT 'new',
    discovered_at      INTEGER,
    updated_at         INTEGER
);
CREATE TABLE IF NOT EXISTS aaibmy_accidents (
    case_id        TEXT PRIMARY KEY,
    event_date     TEXT,
    aircraft       TEXT,
    registration   TEXT,
    operator       TEXT,
    location       TEXT,
    country        TEXT DEFAULT 'MY',
    narrative_text TEXT,
    probable_cause TEXT,
    source_url     TEXT,
    report_type    TEXT,
    site_slug      TEXT,
    built_at       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_aaibmy_reports_status ON aaibmy_reports(status);
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
