# aaiahk_ingest/db.py
import sqlite3
import time

STATUS_NEW = "new"
STATUS_FETCHED = "fetched"
STATUS_PARSED = "parsed"
STATUS_BUILT = "built"
STATUS_SKIPPED = "skipped"

SCHEMA = """
CREATE TABLE IF NOT EXISTS aaiahk_reports (
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
    updated_at         INTEGER,
    superseded_by      TEXT
);
CREATE TABLE IF NOT EXISTS aaiahk_accidents (
    case_id        TEXT PRIMARY KEY,
    event_date     TEXT,
    aircraft       TEXT,
    registration   TEXT,
    operator       TEXT,
    location       TEXT,
    country        TEXT DEFAULT 'HK',
    narrative_text TEXT,
    probable_cause TEXT,
    source_url     TEXT,
    report_type    TEXT,
    site_slug      TEXT,
    built_at       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_aaiahk_reports_status ON aaiahk_reports(status);
"""

# Migrations applied to existing DBs that were created before a schema column
# was added.  Each entry is (column, table, DDL-fragment).  Applied idempotently
# by catching the "duplicate column" error from SQLite.
_MIGRATIONS = [
    # 001 – dedup: track when an ITR/PLR row is superseded by a final IVR
    (
        "superseded_by",
        "aaiahk_reports",
        "ALTER TABLE aaiahk_reports ADD COLUMN superseded_by TEXT",
    ),
]


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_schema(conn):
    conn.executescript(SCHEMA)
    # Apply incremental migrations idempotently
    for _col, _tbl, ddl in _MIGRATIONS:
        try:
            conn.execute(ddl)
            conn.commit()
        except Exception:
            # Column already exists — no-op
            pass


def now_ms():
    return int(time.time() * 1000)
