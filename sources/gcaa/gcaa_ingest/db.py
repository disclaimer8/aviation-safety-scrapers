# gcaa_ingest/db.py
import sqlite3
import time

STATUS_NEW = "new"
STATUS_PARSED = "parsed"
STATUS_BUILT = "built"
STATUS_SKIPPED = "skipped"

SCHEMA = """
CREATE TABLE IF NOT EXISTS gcaa_reports (
    case_id              TEXT PRIMARY KEY,
    reference_no         TEXT,
    item_id              INTEGER,
    filename             TEXT,
    server_relative_url  TEXT,
    aircraft             TEXT,
    registration         TEXT,
    occurrence_category  TEXT,
    report_status        TEXT,
    date_of_occurrence   TEXT,
    location             TEXT,
    damage               TEXT,
    year                 TEXT,
    pdf_url              TEXT,
    pdf_path             TEXT,
    narrative_text       TEXT,
    source_tier          TEXT,
    status               TEXT NOT NULL DEFAULT 'new',
    discovered_at        INTEGER,
    updated_at           INTEGER
);
CREATE TABLE IF NOT EXISTS gcaa_accidents (
    case_id        TEXT PRIMARY KEY,
    event_date     TEXT,
    aircraft       TEXT,
    registration   TEXT,
    operator       TEXT,
    location       TEXT,
    country        TEXT DEFAULT 'AE',
    narrative_text TEXT,
    probable_cause TEXT,
    source_url     TEXT,
    report_type    TEXT,
    site_slug      TEXT,
    built_at       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_gcaa_reports_status ON gcaa_reports(status);
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
