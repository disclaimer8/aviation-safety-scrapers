# jiaacve_ingest/db.py
import sqlite3
import time

STATUS_NEW = "new"
STATUS_FETCHED = "fetched"
STATUS_PARSED = "parsed"
STATUS_BUILT = "built"
STATUS_SKIPPED = "skipped"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jiaacve_reports (
    dl_id              TEXT PRIMARY KEY,  -- WordPress download ID (e.g. '189736')
    case_id            TEXT,              -- NNN/YYYY (may be shared by prelim + final)
    pdf_url            TEXT,              -- direct /download/<id>/ URL
    pdf_path           TEXT,
    listing_title      TEXT,              -- raw text from listing (e.g. 'Informe 001_2026 YV2988 Preliminar')
    listing_year       INTEGER,           -- year from accordion section
    report_type_raw    TEXT,              -- 'Final'|'Preliminar'|'Provisional'|'Expediente'|''
    superseded_by      TEXT,              -- case_id of a better (final) report for same expediente
    event_date         TEXT,              -- ISO YYYY-MM-DD extracted from PDF
    aircraft           TEXT,
    registration       TEXT,
    operator           TEXT,
    location           TEXT,
    narrative_text     TEXT,
    probable_cause     TEXT,
    source_tier        TEXT,              -- 'pdf'|'scanned'|'none'
    fatalities_total   INTEGER,           -- parsed from PDF (may be NULL)
    phase              TEXT,              -- phase of flight from PDF
    status             TEXT NOT NULL DEFAULT 'new',
    discovered_at      INTEGER,
    updated_at         INTEGER
);

CREATE TABLE IF NOT EXISTS jiaacve_accidents (
    case_id          TEXT PRIMARY KEY,
    event_date       TEXT,
    aircraft         TEXT,
    registration     TEXT,
    operator         TEXT,
    location         TEXT,
    country          TEXT DEFAULT 'VE',
    narrative_text   TEXT,
    probable_cause   TEXT,
    source_url       TEXT,
    report_type      TEXT,
    site_slug        TEXT,
    lang             TEXT DEFAULT 'es',
    built_at         INTEGER,
    fatalities_total INTEGER,
    phase            TEXT,
    category         TEXT
);

CREATE INDEX IF NOT EXISTS idx_jiaacve_reports_status   ON jiaacve_reports(status);
CREATE INDEX IF NOT EXISTS idx_jiaacve_reports_case_id  ON jiaacve_reports(case_id);
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
