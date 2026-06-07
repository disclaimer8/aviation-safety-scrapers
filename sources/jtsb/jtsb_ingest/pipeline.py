# jtsb_ingest/pipeline.py
"""
discover → fetch → parse → build pipeline for JTSB (Japan) listing.

Notes:
  discover(): walks the JTSB listing page, inserts new case_ids into
  jtsb_reports with full listing metadata.  Idempotent — existing case_ids
  are skipped.

  fetch(): for each status='new' row that has a pdf_url: downloads the EN PDF
  and advances to 'fetched'.  Rows without a pdf_url are also advanced to
  'fetched' with pdf_path=None so that parse() can handle them gracefully.
  Per-row try/except: a download failure keeps the row at 'new' for the next
  run (does NOT advance).

  parse(): extracts text from the EN PDF via pdftotext.  If text meets
  MIN_NARRATIVE (600 chars) → source_tier='pdf', else tier is 'scanned' when
  there is any text, 'none' when there is none.

  build(): emits jtsb_accidents rows.  Rows whose narrative_text is shorter
  than _NARRATIVE_FLOOR (80 chars), or whose source_tier is not 'pdf', are
  skipped.
"""
import os
import sys
import time

from . import jtsb, db, text
from .pdf import extract_text, MIN_NARRATIVE

_NARRATIVE_FLOOR = 80  # chars; rows with less are treated as non-report events


def discover(conn, client, full=False):
    """
    Walk the JTSB listing page and INSERT new case_ids into jtsb_reports.

    full: accepted for API parity; currently has no extra effect.

    Returns: number of rows inserted.
    """
    rows = jtsb.iter_index(client)

    inserted = 0
    for row in rows:
        case_id = row["case_id"]
        if conn.execute(
            "SELECT 1 FROM jtsb_reports WHERE case_id=?", (case_id,)
        ).fetchone():
            continue  # already known

        ts = db.now_ms()
        conn.execute(
            "INSERT INTO jtsb_reports "
            "(case_id, report_url, pdf_url, jp_pdf_url, "
            "title, report_type, category, flight_phase, "
            "aircraft, registration, date_of_occurrence, "
            "location, operator, status, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                case_id,
                row.get("report_url"),
                row.get("pdf_url"),
                row.get("jp_pdf_url"),
                row.get("title"),
                row.get("report_type"),
                row.get("category"),
                row.get("flight_phase"),
                row.get("aircraft"),
                row.get("registration"),
                row.get("date_of_occurrence"),
                row.get("location"),
                row.get("operator"),
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
    For each status='new' row: download the EN PDF (if pdf_url is set) and
    advance to 'fetched'.

    Returns: number of rows iterated (including failures).
    """
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, pdf_url FROM jtsb_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()

    for row in rows:
        case_id = row["case_id"]
        pdf_url = row["pdf_url"]

        pdf_path = None
        if pdf_url:
            safe_case_id = case_id.replace("/", "_").replace(" ", "_")
            dest = os.path.join(pdf_dir, safe_case_id + ".pdf")
            try:
                time.sleep(jtsb.DELAY)
                jtsb.download(client, pdf_url, dest)
                pdf_path = dest
            except Exception as exc:
                print(f"[jtsb fetch] {case_id}: download {exc}", file=sys.stderr)
                # stay at 'new' for retry — do NOT advance
                continue

        try:
            conn.execute(
                "UPDATE jtsb_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[jtsb fetch] {case_id}: db {exc}", file=sys.stderr)

    return len(rows)


def parse(conn):
    """
    For each status='fetched' row: extract text from the EN PDF (if present).

    source_tier:
      'pdf'     — text length >= MIN_NARRATIVE (600 chars)
      'scanned' — text present but below threshold
      'none'    — no text at all (no PDF or empty extraction)

    Returns: number of rows processed.
    """
    rows = conn.execute(
        "SELECT case_id, pdf_path FROM jtsb_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    for row in rows:
        pdf_path = row["pdf_path"]
        if pdf_path:
            full_text = extract_text(pdf_path)
        else:
            full_text = ""

        if len(full_text) >= MIN_NARRATIVE:
            narrative = full_text
            tier = "pdf"
        elif full_text:
            narrative = full_text
            tier = "scanned"
        else:
            narrative = ""
            tier = "none"

        conn.execute(
            "UPDATE jtsb_reports "
            "SET narrative_text=?, source_tier=?, status=?, updated_at=? "
            "WHERE case_id=?",
            (narrative, tier, db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()

    return len(rows)


def build(conn):
    """
    For each status='parsed' row: emit a jtsb_accidents record or skip.

    Skip criteria (status → 'skipped'):
      • narrative_text shorter than _NARRATIVE_FLOOR chars, OR
      • source_tier != 'pdf' (scanned / none rows are not publishable).

    source_url: pdf_url if present, else report_url.

    Returns: number of rows built (not skipped).
    """
    rows = conn.execute(
        "SELECT case_id, report_type, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, pdf_url, report_url, source_tier "
        "FROM jtsb_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR or row["source_tier"] != "pdf":
            conn.execute(
                "UPDATE jtsb_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        source_url = row["pdf_url"] or row["report_url"]
        site_slug = text.make_site_slug(row["aircraft"], row["registration"], row["location"])

        conn.execute(
            "INSERT OR REPLACE INTO jtsb_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, country, "
            "narrative_text, probable_cause, source_url, report_type, site_slug, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["case_id"],
                row["date_of_occurrence"],
                row["aircraft"],
                row["registration"],
                row["operator"],
                row["location"],
                "JP",
                narrative,
                None,
                source_url,
                row["report_type"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE jtsb_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    return built
