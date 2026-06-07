# ciaado_ingest/db.py
import sqlite3
import time

STATUS_NEW = "new"
STATUS_FETCHED = "fetched"
STATUS_PARSED = "parsed"
STATUS_BUILT = "built"
STATUS_SKIPPED = "skipped"

# NOTE: ciaado_accidents column set is IDENTICAL to ciaiac_accidents
# (the P2 prod-sync step depends on this exact shape).
SCHEMA = """
CREATE TABLE IF NOT EXISTS ciaado_reports (
    case_id            TEXT PRIMARY KEY,
    report_url         TEXT,
    pdf_url            TEXT,
    pdf_url_es         TEXT,
    pdf_url_en         TEXT,
    pdf_path           TEXT,
    title              TEXT,
    event_class        TEXT,
    aircraft           TEXT,
    registration       TEXT,
    date_of_occurrence TEXT,
    location           TEXT,
    operator           TEXT,
    narrative_text     TEXT,
    source_tier        TEXT,
    lang               TEXT,
    status             TEXT NOT NULL DEFAULT 'new',
    discovered_at      INTEGER,
    updated_at         INTEGER
);
CREATE TABLE IF NOT EXISTS ciaado_accidents (
    case_id        TEXT PRIMARY KEY,
    event_date     TEXT,
    aircraft       TEXT,
    registration   TEXT,
    operator       TEXT,
    location       TEXT,
    country        TEXT DEFAULT 'DO',
    narrative_text TEXT,
    probable_cause TEXT,
    source_url     TEXT,
    report_type    TEXT,
    site_slug      TEXT,
    built_at       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ciaado_reports_status ON ciaado_reports(status);
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
