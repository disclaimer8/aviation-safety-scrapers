# uzpln_ingest/db.py
import sqlite3
import time

STATUS_NEW = "new"
STATUS_PARSED = "parsed"
STATUS_BUILT = "built"
STATUS_SKIPPED = "skipped"

SCHEMA = """
CREATE TABLE IF NOT EXISTS uzpln_reports (
    case_id            TEXT PRIMARY KEY,
    incident_id        TEXT UNIQUE,
    pdf_url            TEXT,
    page_url           TEXT,
    report_number      TEXT,
    report_kind        TEXT,
    event_kind         TEXT,
    operation          TEXT,
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
CREATE TABLE IF NOT EXISTS uzpln_accidents (
    case_id        TEXT PRIMARY KEY,
    event_date     TEXT,
    aircraft       TEXT,
    registration   TEXT,
    operator       TEXT,
    location       TEXT,
    country        TEXT DEFAULT 'CZ',
    lang           TEXT DEFAULT 'cs',
    narrative_text TEXT,
    probable_cause TEXT,
    source_url     TEXT,
    report_type    TEXT,
    site_slug      TEXT,
    built_at       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_uzpln_reports_status ON uzpln_reports(status);
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
