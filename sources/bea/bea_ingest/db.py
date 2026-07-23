# bea_ingest/db.py
import sqlite3
import time

STATUS_NEW = "new"
STATUS_FETCHED = "fetched"
STATUS_PARSED = "parsed"
STATUS_BUILT = "built"
STATUS_SKIPPED = "skipped"

SCHEMA = """
CREATE TABLE IF NOT EXISTS bea_reports (
    slug               TEXT PRIMARY KEY,
    detail_url         TEXT,
    title              TEXT,
    event_class        TEXT,
    aircraft_type      TEXT,
    registration       TEXT,
    date_of_occurrence TEXT,
    location           TEXT,
    operator           TEXT,
    pdf_url            TEXT,
    pdf_path           TEXT,
    narrative_text     TEXT,
    source_tier        TEXT,
    status             TEXT NOT NULL DEFAULT 'new',
    discovered_at      INTEGER,
    updated_at         INTEGER,
    last_refetch_at    INTEGER
);
CREATE TABLE IF NOT EXISTS bea_accidents (
    case_id        TEXT PRIMARY KEY,
    event_date     TEXT,
    aircraft       TEXT,
    registration   TEXT,
    operator       TEXT,
    location       TEXT,
    country        TEXT DEFAULT 'FR',
    narrative_text TEXT,
    probable_cause TEXT,
    source_url     TEXT,
    report_type    TEXT,
    site_slug      TEXT,
    built_at       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_reports_status ON bea_reports(status);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_schema(conn):
    conn.executescript(SCHEMA)
    # Migration for DBs created before the stub-refetch stage (2026-07):
    # CREATE TABLE IF NOT EXISTS leaves an existing table untouched, so the
    # column must be added explicitly. PRAGMA-guarded to stay idempotent.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(bea_reports)")}
    if "last_refetch_at" not in cols:
        conn.execute("ALTER TABLE bea_reports ADD COLUMN last_refetch_at INTEGER")
    conn.commit()


def now_ms():
    return int(time.time() * 1000)
