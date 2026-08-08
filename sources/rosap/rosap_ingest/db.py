# rosap_ingest/db.py
import sqlite3
import time

STATUS_NEW = "new"
STATUS_FETCHED = "fetched"
STATUS_PARSED = "parsed"
STATUS_BUILT = "built"
STATUS_SKIPPED = "skipped"

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS rosap_reports (
    pid                TEXT PRIMARY KEY,
    title              TEXT,
    operator           TEXT,
    location           TEXT,
    date_of_occurrence TEXT,
    -- 'report' for the investigation itself; the bracketed label
    -- ([Amendment], [Hearing Notice], [Letter from …]) for a document
    -- attached to a case. Supplements are never built as accidents: five of
    -- them belong to one 1954 accident, and treating them as separate events
    -- would invent four duplicates.
    doc_kind           TEXT,
    pdf_url            TEXT,
    pdf_path           TEXT,
    pdf_bytes          INTEGER,
    narrative_text     TEXT,
    source_tier        TEXT,
    status             TEXT NOT NULL DEFAULT 'new',
    discovered_at      INTEGER,
    updated_at         INTEGER
);

CREATE TABLE IF NOT EXISTS rosap_accidents (
    case_id        TEXT PRIMARY KEY,
    event_date     TEXT,
    aircraft       TEXT,
    registration   TEXT,
    operator       TEXT,
    location       TEXT,
    -- NULL when the location does not resolve. This collection follows US
    -- operators worldwide, so a constant country would be wrong for the
    -- Shannon and Damascus entries.
    country        TEXT,
    narrative_text TEXT,
    probable_cause TEXT,
    source_url     TEXT,
    report_type    TEXT,
    site_slug      TEXT,
    built_at       INTEGER
);

CREATE INDEX IF NOT EXISTS idx_rosap_reports_status ON rosap_reports(status);
CREATE INDEX IF NOT EXISTS idx_rosap_reports_kind ON rosap_reports(doc_kind);
"""


def now_ms():
    return int(time.time() * 1000)


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()
