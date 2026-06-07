# tsb_ingest/pipeline.py
"""
discover → fetch+parse → build pipeline for TSB (Transportation Safety Board of Canada).

discover() walks the TSB aviation index (single-page DataTables) and inserts
new case_ids into tsb_reports along with all available index metadata.  Walk
is always exhaustive (skip-existing per row, no early-break) because the
listing is newest-first but we cannot rely on consecutive-known as a stop signal.

fetch() downloads and immediately parses each report's HTML in one step.
There is no separate PDF artifact — TSB reports are HTML-only — so there is no
intermediate 'fetched' status; rows go directly from 'new' to 'parsed'.

build() promotes 'parsed' rows whose narrative meets _NARRATIVE_FLOOR into
tsb_accidents records.
"""
import sys
import time

from . import db, tsb
from .text import make_site_slug

_NARRATIVE_FLOOR = 80  # chars; rows with less are treated as empty / no-content


def discover(conn, client, full=False):
    """
    Walk the TSB aviation index and INSERT new case_ids into tsb_reports.

    All index metadata (report_url, occurrence_type, operator, aircraft,
    location, date_of_occurrence, occurrence_status) is stored on first sight.

    Per-row skip: if the case_id is already present in tsb_reports, skip it.
    We do NOT early-break on consecutive-known rows.

    full: accepted for API parity; no extra effect (whole index always walked).

    Returns: number of rows inserted.
    """
    inserted = 0
    for r in tsb.iter_index(client):
        case_id = r["case_id"]
        if conn.execute(
            "SELECT 1 FROM tsb_reports WHERE case_id=?", (case_id,)
        ).fetchone():
            continue  # already known — skip, keep walking
        ts = db.now_ms()
        conn.execute(
            "INSERT INTO tsb_reports "
            "(case_id, report_url, title, occurrence_type, aircraft, "
            "registration, date_of_occurrence, location, operator, "
            "occurrence_status, status, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                case_id,
                r["report_url"],
                r.get("occurrence_type"),   # use as title placeholder
                r.get("occurrence_type"),
                r.get("aircraft"),
                r.get("registration"),      # may be None — TSB index rarely includes reg
                r.get("event_date"),        # stored as date_of_occurrence
                r.get("location"),
                r.get("operator"),
                r.get("occurrence_status"),
                db.STATUS_NEW,
                ts,
                ts,
            ),
        )
        inserted += 1
    conn.commit()
    return inserted


def fetch(conn, client):
    """
    For each status='new' row: HTTP GET the report page, parse its narrative,
    and advance to 'parsed'.

    Fetch + parse are combined in one step because both are cheap HTML
    operations — there is no PDF download artifact to store separately.

    Per-row try/except ensures one bad fetch/parse never aborts the batch.
    A failing row stays at 'new' for retry on the next run.

    Returns: number of rows iterated (not just successful ones).
    """
    rows = conn.execute(
        "SELECT case_id, report_url FROM tsb_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()
    for row in rows:
        case_id = row["case_id"]
        report_url = row["report_url"]
        time.sleep(tsb.DELAY)
        try:
            html = tsb.fetch_report(client, report_url)
            narrative = tsb.parse_report(html)
        except Exception as e:
            print(f"[tsb fetch] {case_id}: failed: {e}", file=sys.stderr)
            continue  # stay 'new' for retry

        try:
            conn.execute(
                "UPDATE tsb_reports "
                "SET narrative_text=?, source_tier=?, status=?, updated_at=? "
                "WHERE case_id=?",
                (narrative, "html", db.STATUS_PARSED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as e:
            print(f"[tsb fetch] {case_id}: db update failed: {e}", file=sys.stderr)

    return len(rows)


def parse(conn):
    """
    No-op: parse is folded into fetch() for TSB (HTML-only, no separate PDF
    parse step).  Provided for CLI parity / scripting convenience.

    Returns: 0 (no additional work performed).
    """
    return 0


def build(conn):
    """
    For each status='parsed' row with a substantive narrative: emit a
    tsb_accidents record.

    Skip (→ 'skipped') rows whose narrative is shorter than _NARRATIVE_FLOOR.

    Returns: number of rows built.
    """
    rows = conn.execute(
        "SELECT case_id, report_url, occurrence_type, aircraft, registration, "
        "location, operator, date_of_occurrence, narrative_text "
        "FROM tsb_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()
    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE tsb_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        site_slug = make_site_slug(row["aircraft"], row["registration"], row["location"])
        conn.execute(
            "INSERT OR REPLACE INTO tsb_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, "
            "country, narrative_text, probable_cause, source_url, report_type, "
            "site_slug, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["case_id"],
                row["date_of_occurrence"],
                row["aircraft"],
                row["registration"],
                row["operator"],
                row["location"],
                "CA",
                narrative,
                None,                   # probable_cause — not extracted from TSB HTML
                row["report_url"] or "",
                row["occurrence_type"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE tsb_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1
    return built
