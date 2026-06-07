# gpiaaf_ingest/db.py
import sqlite3
import time

STATUS_NEW = "new"
STATUS_FETCHED = "fetched"
STATUS_PARSED = "parsed"
STATUS_BUILT = "built"
STATUS_SKIPPED = "skipped"
#: Metadata-only rows whose ONLY Documento is a quarterly bulletin — kept for
#: provenance but never fetched/built.
STATUS_NO_REPORT = "no_report"

SCHEMA = """
CREATE TABLE IF NOT EXISTS gpiaaf_reports (
    case_id            TEXT PRIMARY KEY,
    doc_url            TEXT,
    pdf_id             TEXT,
    pdf_url            TEXT,
    pdf_path           TEXT,
    year               TEXT,
    source_url         TEXT,
    classification     TEXT,
    aircraft           TEXT,
    registration       TEXT,
    location           TEXT,
    event_date         TEXT,
    narrative_text     TEXT,
    source_tier        TEXT,
    lang               TEXT DEFAULT 'pt',
    status             TEXT NOT NULL DEFAULT 'new',
    discovered_at      INTEGER,
    updated_at         INTEGER
);
CREATE TABLE IF NOT EXISTS gpiaaf_accidents (
    case_id        TEXT PRIMARY KEY,
    event_date     TEXT,
    aircraft       TEXT,
    registration   TEXT,
    operator       TEXT,
    location       TEXT,
    country        TEXT DEFAULT 'PT',
    narrative_text TEXT,
    probable_cause TEXT,
    source_url     TEXT,
    report_type    TEXT,
    site_slug      TEXT,
    built_at       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_gpiaaf_reports_status ON gpiaaf_reports(status);
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
