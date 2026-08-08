# ntsbcarol_ingest/db.py
"""SQLite schema and helpers for ntsbcarol (NTSB board-level CAROL investigations)."""
import sqlite3
import time

STATUS_NEW = "new"
STATUS_FETCHED = "fetched"
STATUS_PARSED = "parsed"
STATUS_BUILT = "built"
STATUS_SKIPPED = "skipped"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ntsbcarol_cases (
    case_id            TEXT PRIMARY KEY,   -- ntsbcarol-<ntsb_no lowercased>
    ntsb_no            TEXT NOT NULL,      -- e.g. DCA17IA148
    mkey               TEXT NOT NULL,      -- CAROL internal key
    report_no          TEXT,               -- e.g. AIR1801 (if available)
    event_date         TEXT,               -- ISO date YYYY-MM-DD
    city               TEXT,
    state              TEXT,
    country            TEXT DEFAULT 'US',
    registration       TEXT,
    make               TEXT,
    model              TEXT,
    event_type         TEXT,               -- Accident / Incident
    highest_injury     TEXT,
    fatalities_total   INTEGER,
    phase              TEXT,
    category           TEXT,
    pdf_url            TEXT,               -- https://www.ntsb.gov/investigations/...pdf
    pdf_path           TEXT,
    narrative_text     TEXT,
    probable_cause     TEXT,
    source_tier        TEXT,               -- 'pdf', 'api', 'none'
    status             TEXT NOT NULL DEFAULT 'new',
    discovered_at      INTEGER,
    updated_at         INTEGER
);

CREATE TABLE IF NOT EXISTS ntsbcarol_accidents (
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
    report_type    TEXT DEFAULT 'final',
    site_slug      TEXT,
    fatalities_total INTEGER,
    phase          TEXT,
    category       TEXT,
    lang           TEXT DEFAULT 'en',
    built_at       INTEGER
);

CREATE INDEX IF NOT EXISTS idx_ntsbcarol_cases_status ON ntsbcarol_cases(status);
CREATE INDEX IF NOT EXISTS idx_ntsbcarol_cases_ntsb_no ON ntsbcarol_cases(ntsb_no);
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
