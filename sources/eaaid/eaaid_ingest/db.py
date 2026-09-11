# eaaid_ingest/db.py
import sqlite3
import time

STATUS_NEW      = "new"
STATUS_FETCHED  = "fetched"
STATUS_PARSED   = "parsed"
STATUS_BUILT    = "built"
STATUS_SKIPPED  = "skipped"

SCHEMA = """
CREATE TABLE IF NOT EXISTS eaaid_reports (
    case_id            TEXT PRIMARY KEY,
    guid               TEXT NOT NULL,
    source_url         TEXT,
    pdf_path           TEXT,
    report_type        TEXT,
    category           TEXT,
    event_date         TEXT,
    location           TEXT,
    aircraft           TEXT,
    registration       TEXT,
    narrative_text     TEXT,
    source_tier        TEXT,
    lang               TEXT DEFAULT 'en',
    status             TEXT NOT NULL DEFAULT 'new',
    skip_reason        TEXT,
    superseded_by      TEXT,
    discovered_at      INTEGER,
    updated_at         INTEGER
);
CREATE TABLE IF NOT EXISTS eaaid_accidents (
    case_id            TEXT PRIMARY KEY,
    event_date         TEXT,
    aircraft           TEXT,
    registration       TEXT,
    operator           TEXT,
    location           TEXT,
    country            TEXT DEFAULT 'EG',
    narrative_text     TEXT,
    probable_cause     TEXT,
    source_url         TEXT,
    report_type        TEXT,
    site_slug          TEXT,
    fatalities_total   INTEGER,
    phase              TEXT,
    category           TEXT,
    built_at           INTEGER
);
CREATE INDEX IF NOT EXISTS idx_eaaid_status    ON eaaid_reports(status);
CREATE INDEX IF NOT EXISTS idx_eaaid_guid      ON eaaid_reports(guid);
CREATE INDEX IF NOT EXISTS idx_eaaid_event_date ON eaaid_reports(event_date);
"""

# Migrations applied idempotently to existing DBs created before a schema
# column was added.  Each entry is (ddl,).  Applied by catching duplicate-column
# errors from SQLite.
_MIGRATIONS = [
    # 001 — superseded_by: interim/preliminary rows superseded by a final report
    ("ALTER TABLE eaaid_reports ADD COLUMN superseded_by TEXT",),
    ("ALTER TABLE eaaid_accidents ADD COLUMN superseded_by TEXT",),
]


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_schema(conn):
    conn.executescript(SCHEMA)
    for (ddl,) in _MIGRATIONS:
        try:
            conn.execute(ddl)
            conn.commit()
        except Exception:
            pass  # column already exists


def now_ms():
    return int(time.time() * 1000)
