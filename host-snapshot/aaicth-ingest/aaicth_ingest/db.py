# aaicth_ingest/db.py
import sqlite3
import time

STATUS_NEW = "new"
STATUS_FETCHED = "fetched"
STATUS_PARSED = "parsed"
STATUS_BUILT = "built"
STATUS_SKIPPED = "skipped"

SCHEMA = """
CREATE TABLE IF NOT EXISTS aaicth_reports (
    case_id         TEXT PRIMARY KEY,
    seq_no          TEXT,
    year            INTEGER,
    report_type     TEXT,
    report_url      TEXT,
    pdf_url         TEXT,
    pdf_url_th      TEXT,
    pdf_url_en      TEXT,
    pdf_path        TEXT,
    title           TEXT,
    aircraft        TEXT,
    registration    TEXT,
    event_date      TEXT,
    narrative_text  TEXT,
    source_tier     TEXT,
    lang            TEXT,
    superseded_by   TEXT,
    status          TEXT NOT NULL DEFAULT 'new',
    discovered_at   INTEGER,
    updated_at      INTEGER
);

CREATE TABLE IF NOT EXISTS aaicth_accidents (
    case_id        TEXT PRIMARY KEY,
    event_date     TEXT,
    aircraft       TEXT,
    registration   TEXT,
    operator       TEXT,
    location       TEXT,
    country        TEXT DEFAULT 'TH',
    narrative_text TEXT,
    probable_cause TEXT,
    source_url     TEXT,
    report_type    TEXT,
    lang           TEXT,
    site_slug      TEXT,
    built_at       INTEGER
);

CREATE INDEX IF NOT EXISTS idx_aaicth_reports_status ON aaicth_reports(status);
CREATE INDEX IF NOT EXISTS idx_aaicth_reports_superseded ON aaicth_reports(superseded_by);
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
