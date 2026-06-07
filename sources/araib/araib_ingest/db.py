# araib_ingest/db.py
import sqlite3
import time

STATUS_NEW = "new"          # discovered from the listing; DTL not yet fetched
STATUS_DETAILED = "detailed"  # DTL parsed, PDF url known; PDF not yet fetched
STATUS_PARSED = "parsed"    # PDF downloaded + text extracted
STATUS_BUILT = "built"
STATUS_SKIPPED = "skipped"

# ⚠️ ARAIB is a 3-stage source (listing -> DTL detail page -> PDF). The stable
# key known at discover time is the numeric listing row id `idx` (NOT case_id —
# the canonical ARAIB case number lives only inside the PDF synopsis and is not
# known until fetch). So `idx` is the PRIMARY KEY; `case_id` is filled in at
# fetch (case number from the synopsis, fallback 'araib-{idx}').
SCHEMA = """
CREATE TABLE IF NOT EXISTS araib_reports (
    idx                TEXT PRIMARY KEY,
    case_id            TEXT,
    dtl_url            TEXT UNIQUE,
    pdf_url            TEXT,
    title              TEXT,
    publish_date       TEXT,
    view_count         TEXT,
    case_number        TEXT,
    registration       TEXT,
    event_date         TEXT,
    operator           TEXT,
    aircraft           TEXT,
    location           TEXT,
    pdf_path           TEXT,
    narrative_text     TEXT,
    source_tier        TEXT,
    status             TEXT NOT NULL DEFAULT 'new',
    discovered_at      INTEGER,
    updated_at         INTEGER
);
CREATE TABLE IF NOT EXISTS araib_accidents (
    case_id        TEXT PRIMARY KEY,
    event_date     TEXT,
    aircraft       TEXT,
    registration   TEXT,
    operator       TEXT,
    location       TEXT,
    country        TEXT DEFAULT 'KR',
    lang           TEXT DEFAULT 'en',
    narrative_text TEXT,
    probable_cause TEXT,
    source_url     TEXT,
    report_type    TEXT,
    site_slug      TEXT,
    built_at       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_araib_reports_status ON araib_reports(status);
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
