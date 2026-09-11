# aacsv_ingest/db.py
import sqlite3
import time

STATUS_NEW = "new"
STATUS_FETCHED = "fetched"
STATUS_PARSED = "parsed"
STATUS_BUILT = "built"
STATUS_SKIPPED = "skipped"

# aacsv_accidents columns are IDENTICAL to ciaiac_accidents (country default 'SV').
# aacsv_reports is keyed by the per-row WPDM slug (always unique on the listing);
# the deduped intrinsic case_id is carried as a column and used as the
# aacsv_accidents PK so that a FINAL report supersedes a PRELIMINARY one for the
# same occurrence (same case base).
SCHEMA = """
CREATE TABLE IF NOT EXISTS aacsv_reports (
    slug               TEXT PRIMARY KEY,
    case_id            TEXT,
    report_url         TEXT,
    pdf_url            TEXT,
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
    report_type        TEXT,
    lang               TEXT,
    status             TEXT NOT NULL DEFAULT 'new',
    discovered_at      INTEGER,
    updated_at         INTEGER
);
CREATE TABLE IF NOT EXISTS aacsv_accidents (
    case_id        TEXT PRIMARY KEY,
    event_date     TEXT,
    aircraft       TEXT,
    registration   TEXT,
    operator       TEXT,
    location       TEXT,
    country        TEXT DEFAULT 'SV',
    narrative_text TEXT,
    probable_cause TEXT,
    source_url     TEXT,
    report_type    TEXT,
    site_slug      TEXT,
    built_at       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_aacsv_reports_status ON aacsv_reports(status);
CREATE INDEX IF NOT EXISTS idx_aacsv_reports_caseid ON aacsv_reports(case_id);
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
