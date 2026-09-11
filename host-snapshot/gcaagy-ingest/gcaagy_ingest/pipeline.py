# gcaagy_ingest/pipeline.py
"""
discover -> fetch -> parse -> build pipeline for GCAA Guyana (single AAID page).

discover(): GET the one AAID.html listing page, parse the <li><a> PDF rows and
  INSERT new case_ids (intrinsic 'gcaagy-<filename-slug>') into gcaagy_reports
  with the PDF url + title.  lang is always 'en' (English source).  Idempotent.

fetch(): for each status='new' row, download the PDF (spaces in the href are
  percent-encoded by the downloader) and advance to 'fetched'.  Per-row
  try/except: a download failure keeps the row at 'new' for the next run.

parse(): extract text via pdftotext.  Scanned-aware:
    text length <  _SCANNED_FLOOR (500) -> tier 'scanned'  (image-only PDF)
    text length >= MIN_NARRATIVE (600)  -> tier 'pdf'
    otherwise                           -> tier 'short'
  Also harvests the official 'AAIIU' file reference (stored in report_url) and
  the first aircraft registration from the report text when not already set.

build(): emit gcaagy_accidents rows (country 'GY').  Rows whose narrative is
  shorter than _NARRATIVE_FLOOR (80) -- which includes every 'scanned' row --
  are skipped.
"""
import os
import sys
import time

from . import gcaagy, db, text
from .pdf import extract_text, MIN_NARRATIVE

_NARRATIVE_FLOOR = 80   # chars; rows with less are non-report events
_SCANNED_FLOOR = 500    # chars; below this an image-only (scanned) PDF is assumed


def discover(conn, client, full=False):
    """
    GET the GCAA AAID index page and INSERT new case_ids into gcaagy_reports.

    full: accepted for API parity (the whole single page is always walked).
    lang is always 'en'.  Returns: number of rows inserted.
    """
    resp = client.get(gcaagy.INDEX_URL)
    resp.raise_for_status()
    index_html = resp.content.decode("utf-8", "replace") if isinstance(resp.content, bytes) else resp.text

    rows = gcaagy.parse_listing(index_html)

    inserted = 0
    for row in rows:
        case_id = row["case_id"]
        if conn.execute(
            "SELECT 1 FROM gcaagy_reports WHERE case_id=?", (case_id,)
        ).fetchone():
            continue  # already known

        ts = db.now_ms()
        conn.execute(
            "INSERT INTO gcaagy_reports "
            "(case_id, pdf_url, title, event_class, lang, status, "
            "discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                case_id,
                row.get("pdf_url"),
                row.get("title"),
                "Accident",
                "en",
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

    Per-row try/except: a download failure keeps the row at 'new' for retry.
    Returns: number of rows iterated (including failures).
    """
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, pdf_url FROM gcaagy_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()

    for row in rows:
        case_id = row["case_id"]
        pdf_url = row["pdf_url"]

        pdf_path = None
        if pdf_url:
            dest = os.path.join(pdf_dir, case_id + ".pdf")
            try:
                time.sleep(gcaagy.DELAY)
                gcaagy.download(client, pdf_url, dest)
                pdf_path = dest
            except Exception as exc:
                print(f"[gcaagy fetch] {case_id}: download {exc}", file=sys.stderr)
                continue  # stay at 'new' for retry

        try:
            conn.execute(
                "UPDATE gcaagy_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[gcaagy fetch] {case_id}: db {exc}", file=sys.stderr)

    return len(rows)


def parse(conn):
    """
    For each status='fetched' row: extract text and classify.

    source_tier:
      'scanned' -- text below _SCANNED_FLOOR (image-only PDF)
      'pdf'     -- text length >= MIN_NARRATIVE
      'short'   -- some text, below MIN_NARRATIVE but >= _SCANNED_FLOOR
      'none'    -- no PDF / empty extraction

    Harvests the AAIIU file reference (into report_url), the first
    registration, and the cover-block fields (event date, aircraft, location,
    operator) from the report text when present and not already set.

    Returns: number of rows processed.
    """
    rows = conn.execute(
        "SELECT case_id, pdf_path, registration, aircraft, location, operator, "
        "date_of_occurrence FROM gcaagy_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    for row in rows:
        pdf_path = row["pdf_path"]
        full_text = extract_text(pdf_path) if pdf_path else ""

        if not full_text:
            narrative, tier = "", "none"
        elif len(full_text) < _SCANNED_FLOOR:
            narrative, tier = full_text, "scanned"
        elif len(full_text) >= MIN_NARRATIVE:
            narrative, tier = full_text, "pdf"
        else:
            narrative, tier = full_text, "short"

        aaiiu = gcaagy.find_aaiiu_ref(full_text)
        registration = row["registration"] or gcaagy.find_registration(full_text)
        # Cover-block fields: extract only when not already set (idempotent).
        event_date = row["date_of_occurrence"] or gcaagy.extract_event_date(full_text)
        aircraft = row["aircraft"] or gcaagy.extract_aircraft(full_text)
        location = row["location"] or gcaagy.extract_location(full_text)
        operator = row["operator"] or gcaagy.extract_operator(full_text)

        conn.execute(
            "UPDATE gcaagy_reports "
            "SET narrative_text=?, source_tier=?, report_url=?, registration=?, "
            "date_of_occurrence=?, aircraft=?, location=?, operator=?, "
            "status=?, updated_at=? WHERE case_id=?",
            (narrative, tier, aaiiu, registration,
             event_date, aircraft, location, operator,
             db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()

    return len(rows)


def build(conn):
    """
    For each status='parsed' row: emit a gcaagy_accidents record or skip.

    Skip (status -> 'skipped') when narrative_text < _NARRATIVE_FLOOR chars
    (this covers every 'scanned' / 'none' row).

    report_type carries the AAIIU file reference when one was found, else the
    event_class.  Returns: number of rows built (not skipped).
    """
    rows = conn.execute(
        "SELECT case_id, event_class, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, pdf_url, report_url "
        "FROM gcaagy_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE gcaagy_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        source_url = row["pdf_url"]
        site_slug = text.make_site_slug(row["aircraft"], row["registration"], row["location"])
        report_type = row["report_url"] or row["event_class"]

        conn.execute(
            "INSERT OR REPLACE INTO gcaagy_accidents "
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
                "GY",
                narrative,
                None,
                source_url,
                report_type,
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE gcaagy_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    return built
