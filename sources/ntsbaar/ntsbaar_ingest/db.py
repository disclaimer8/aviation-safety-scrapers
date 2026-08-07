# ntsbaar_ingest/db.py
import sqlite3
import time

STATUS_NEW = "new"
STATUS_FETCHED = "fetched"
STATUS_PARSED = "parsed"
STATUS_BUILT = "built"
STATUS_SKIPPED = "skipped"

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS ntsbaar_reports (
    case_id        TEXT PRIMARY KEY,   -- AAR7226
    pdf_url        TEXT UNIQUE,
    adopted_year   INTEGER,            -- when the Board adopted it, NOT the event
    pdf_path       TEXT,
    pdf_bytes      INTEGER,
    narrative_text TEXT,
    -- 'pdf' when the file had a text layer, 'ocr' when it had to be scanned,
    -- 'scanned' when neither recovered a usable narrative. About a third of
    -- the collection carries text; the 1970s and 80s are images.
    source_tier    TEXT,
    status         TEXT NOT NULL DEFAULT 'new',
    discovered_at  INTEGER,
    updated_at     INTEGER
);

CREATE TABLE IF NOT EXISTS ntsbaar_accidents (
    case_id        TEXT PRIMARY KEY,
    event_date     TEXT,
    aircraft       TEXT,
    registration   TEXT,
    operator       TEXT,
    location       TEXT,
    country        TEXT DEFAULT 'US',
    narrative_text TEXT,
    probable_cause TEXT,
    source_url     TEXT,
    report_type    TEXT,
    site_slug      TEXT,
    built_at       INTEGER
);

CREATE INDEX IF NOT EXISTS idx_ntsbaar_reports_status ON ntsbaar_reports(status);
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
