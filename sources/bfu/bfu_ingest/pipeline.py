# bfu_ingest/pipeline.py
"""
discover → fetch → parse → build pipeline for BFU Untersuchungsberichte.

discover() walks the paginated BFU search results and inserts new case_ids
into bfu_reports.  Walk is always exhaustive (skip-existing per row, no
early-break) because the listing is newest-first but we can't rely on
consecutive-known to be a reliable stop signal.

build() skip threshold: rows whose narrative is shorter than _NARRATIVE_FLOOR
are delegated / empty and have nothing meaningful to surface to users.
"""
import os
import re
import sys
import time

from . import bfu, db, header
from .pdf import extract_text, MIN_NARRATIVE
from .text import make_site_slug

_NARRATIVE_FLOOR = 80  # chars; rows with less are treated as non-report events

# Characters not safe for a filename (on POSIX or Windows)
_UNSAFE_FILENAME_RE = re.compile(r'[/\\:*?"<>|]')


def _safe_filename(case_id: str) -> str:
    """Replace unsafe filename characters with underscores."""
    return _UNSAFE_FILENAME_RE.sub("_", case_id)


def discover(conn, client, full=False):
    """
    Walk the BFU search results and INSERT new case_ids into bfu_reports.

    Per-row skip: if the case_id is already present in bfu_reports, skip it.
    We do NOT early-break on consecutive-known rows.

    full: accepted for API parity; no extra effect (whole listing always walked).

    Returns: number of rows inserted.
    """
    inserted = 0
    for r in bfu.iter_reports(client):
        case_id = r["case_id"]
        if conn.execute(
            "SELECT 1 FROM bfu_reports WHERE case_id=?", (case_id,)
        ).fetchone():
            continue  # already known — skip, keep walking
        ts = db.now_ms()
        conn.execute(
            "INSERT INTO bfu_reports "
            "(case_id, detail_url, title, status, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                case_id,
                r["pdf_url"],   # store the PDF URL in detail_url column
                r["title"],
                db.STATUS_NEW,
                ts,
                ts,
            ),
        )
        inserted += 1
    conn.commit()
    return inserted


def fetch(conn, client, pdf_dir):
    """
    For each status='new' row: download the PDF and advance to 'fetched'.

    Per-row try/except ensures one bad download never aborts the batch.
    A failing download leaves the row at 'new' for retry on the next run.

    Returns: number of rows iterated (not just successful ones).
    """
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, detail_url FROM bfu_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()
    for row in rows:
        case_id = row["case_id"]
        pdf_url = row["detail_url"]
        pdf_path = None
        # Throttle PDF downloads — BFU rate-limits bursts (WAF → 403). Same
        # politeness delay as the page walk in bfu.iter_reports.
        time.sleep(bfu.DELAY)
        try:
            dest = os.path.join(pdf_dir, _safe_filename(case_id) + ".pdf")
            bfu.download(client, pdf_url, dest)
            pdf_path = dest
        except Exception as e:
            print(f"[bfu fetch] {case_id}: download failed: {e}", file=sys.stderr)
            continue  # stay 'new' for retry

        try:
            conn.execute(
                "UPDATE bfu_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as e:
            print(f"[bfu fetch] {case_id}: db update failed: {e}", file=sys.stderr)

    return len(rows)


def parse(conn):
    """
    For each status='fetched' row: extract text from PDF and populate metadata.

    If PDF text meets MIN_NARRATIVE threshold → tier='pdf' and header fields
    (event_class, aircraft, registration, date_iso, location) are parsed from
    the Identifikation block.
    Otherwise narrative is empty and tier='none'.

    The header's case_id (Aktenzeichen) is stored as a separate column via
    the existing `pdf_url` / `pdf_path` schema; we keep bfu_reports.case_id
    stable (PK).  The canonical Aktenzeichen from the header is available at
    build time by re-calling parse_header on the stored narrative_text.

    Returns: number of rows processed.
    """
    rows = conn.execute(
        "SELECT case_id, pdf_path FROM bfu_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()
    for row in rows:
        full_text = extract_text(row["pdf_path"]) if row["pdf_path"] else ""
        if len(full_text) >= MIN_NARRATIVE:
            narrative, tier = full_text, "pdf"
            h = header.parse_header(narrative)
            event_class  = h.get("event_class")
            aircraft     = h.get("aircraft")
            registration = h.get("registration")
            date_iso     = h.get("date_iso")
            location     = h.get("location")
        else:
            narrative, tier = "", "none"
            event_class = aircraft = registration = date_iso = location = None

        conn.execute(
            "UPDATE bfu_reports "
            "SET narrative_text=?, source_tier=?, event_class=?, aircraft=?, "
            "registration=?, date_of_occurrence=?, location=?, status=?, updated_at=? "
            "WHERE case_id=?",
            (
                narrative, tier, event_class, aircraft, registration,
                date_iso, location, db.STATUS_PARSED, db.now_ms(), row["case_id"],
            ),
        )
        conn.commit()
    return len(rows)


def build(conn):
    """
    For each status='parsed' row with a substantive narrative: emit a
    bfu_accidents record.

    The canonical Aktenzeichen is derived by re-parsing the stored
    narrative_text with header.parse_header().  If the header yields a
    case_id, that becomes bfu_accidents.case_id; otherwise the staging
    row's case_id is used.  This keeps bfu_reports.case_id stable as the PK
    while bfu_accidents.case_id carries the PDF-canonical value.

    Skip (→ 'skipped') rows whose narrative is shorter than _NARRATIVE_FLOOR
    (delegated / empty reports).

    Returns: number of rows built.
    """
    rows = conn.execute(
        "SELECT case_id, aircraft, registration, location, date_of_occurrence, "
        "narrative_text, event_class, detail_url "
        "FROM bfu_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()
    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE bfu_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        # Derive canonical Aktenzeichen from header; fall back to staging PK
        h = header.parse_header(narrative)
        canonical_case_id = h.get("case_id") or row["case_id"]

        site_slug = make_site_slug(row["aircraft"], row["registration"], row["location"])
        conn.execute(
            "INSERT OR REPLACE INTO bfu_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, country, "
            "narrative_text, probable_cause, source_url, report_type, site_slug, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                canonical_case_id,
                row["date_of_occurrence"],
                row["aircraft"],
                row["registration"],
                None,           # operator — BFU doesn't publish in Identifikation
                row["location"],
                "DE",
                narrative,
                None,           # probable_cause
                row["detail_url"] or "",
                row["event_class"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE bfu_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1
    return built
