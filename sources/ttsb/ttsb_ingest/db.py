# ttsb_ingest/db.py
import sqlite3
import time

STATUS_NEW = "new"
STATUS_PARSED = "parsed"
STATUS_BUILT = "built"
STATUS_SKIPPED = "skipped"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ttsb_reports (
    case_id            TEXT PRIMARY KEY,
    detail_id          TEXT UNIQUE,
    en_detail_url      TEXT,
    zh_detail_url      TEXT,
    en_pdf_url         TEXT,
    zh_pdf_url         TEXT,
    title              TEXT,
    report_kind        TEXT,
    aircraft           TEXT,
    registration       TEXT,
    date_of_occurrence TEXT,
    location           TEXT,
    lang               TEXT,
    en_pdf_path        TEXT,
    zh_pdf_path        TEXT,
    narrative_text     TEXT,
    en_summary_text    TEXT,
    source_tier        TEXT,
    status             TEXT NOT NULL DEFAULT 'new',
    discovered_at      INTEGER,
    updated_at         INTEGER
);
CREATE TABLE IF NOT EXISTS ttsb_accidents (
    case_id        TEXT PRIMARY KEY,
    event_date     TEXT,
    aircraft       TEXT,
    registration   TEXT,
    operator       TEXT,
    location       TEXT,
    country        TEXT DEFAULT 'TW',
    lang           TEXT,
    narrative_text TEXT,
    en_summary_text TEXT,
    probable_cause TEXT,
    source_url     TEXT,
    report_type    TEXT,
    site_slug      TEXT,
    built_at       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ttsb_reports_status ON ttsb_reports(status);
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
