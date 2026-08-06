# ahac_ingest/db.py
import sqlite3
import time

STATUS_NEW = "new"
STATUS_FETCHED = "fetched"
STATUS_PARSED = "parsed"
STATUS_BUILT = "built"
STATUS_SKIPPED = "skipped"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ahac_reports (
    case_id            TEXT PRIMARY KEY,
    pdf_url            TEXT,
    pdf_path           TEXT,
    title              TEXT,
    event_class        TEXT,
    aircraft           TEXT,
    registration       TEXT,
    date_of_occurrence TEXT,
    location           TEXT,
    narrative_text     TEXT,
    source_tier        TEXT,
    lang               TEXT DEFAULT 'es',
    status             TEXT NOT NULL DEFAULT 'new',
    discovered_at      INTEGER,
    updated_at         INTEGER
);
CREATE TABLE IF NOT EXISTS ahac_accidents (
    case_id        TEXT PRIMARY KEY,
    event_date     TEXT,
    aircraft       TEXT,
    registration   TEXT,
    location       TEXT,
    country        TEXT DEFAULT 'HN',
    narrative_text TEXT,
    source_url     TEXT,
    report_type    TEXT,
    site_slug      TEXT,
    built_at       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ahac_reports_status ON ahac_reports(status);
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
