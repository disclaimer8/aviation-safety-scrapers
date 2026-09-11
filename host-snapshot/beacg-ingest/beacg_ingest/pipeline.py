# beacg_ingest/pipeline.py
"""
discover -> fetch -> parse -> build pipeline for BEA Congo (single index page).

discover(): GET the one index page, parse the table rows and INSERT new
  case_ids (intrinsic 'beacg-<final-pdf-filename-slug>') into beacg_reports
  with the final-report PDF url, title and the structured listing fields
  (BEA ref -> report_url, aircraft, event date, location, category).  Only
  rows with a final-report PDF are inserted.  lang is always 'fr'.  Idempotent.

fetch(): for each status='new' row, download the final-report PDF (the href is
  percent-encoded by the downloader) and advance to 'fetched'.  Per-row
  try/except: a download failure keeps the row at 'new' for the next run.

parse(): extract text via pdftotext.  Scanned-aware:
    text length <  _SCANNED_FLOOR (500) -> tier 'scanned'  (image-only PDF)
    text length >= MIN_NARRATIVE (600)  -> tier 'pdf'
    otherwise                           -> tier 'short'
  Backfills the BEA file reference (report_url), the first registration and
  the cover-block fields (event date, aircraft, location, operator) from the
  report text only when the listing did not already supply them.

build(): emit beacg_accidents rows (country 'CG').  Rows whose narrative is
  shorter than _NARRATIVE_FLOOR (80) -- which includes every 'scanned' row --
  are skipped.
"""
import os
import sys
import time

from . import beacg, db, text
from .pdf import extract_text, MIN_NARRATIVE

_NARRATIVE_FLOOR = 80   # chars; rows with less are non-report events
_SCANNED_FLOOR = 500    # chars; below this an image-only (scanned) PDF is assumed


def discover(conn, client, full=False):
    """
    GET the BEA Congo index page and INSERT new case_ids into beacg_reports.

    full: accepted for API parity (the whole single page is always walked).
    lang is always 'fr'.  Returns: number of rows inserted.
    """
    resp = client.get(beacg.INDEX_URL)
    resp.raise_for_status()
    index_html = resp.content.decode("utf-8", "replace") if isinstance(resp.content, bytes) else resp.text

    rows = beacg.parse_listing(index_html)

    inserted = 0
    for row in rows:
        case_id = row["case_id"]
        if conn.execute(
            "SELECT 1 FROM beacg_reports WHERE case_id=?", (case_id,)
        ).fetchone():
            continue  # already known

        ts = db.now_ms()
        conn.execute(
            "INSERT INTO beacg_reports "
            "(case_id, pdf_url, title, report_url, event_class, aircraft, "
            "date_of_occurrence, location, lang, status, "
            "discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                case_id,
                row.get("pdf_url"),
                row.get("title"),
                row.get("bea_ref"),
                row.get("event_class") or "Accident",
                row.get("aircraft"),
                row.get("event_date"),
                row.get("location"),
                "fr",
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
        "SELECT case_id, pdf_url FROM beacg_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()

    for row in rows:
        case_id = row["case_id"]
        pdf_url = row["pdf_url"]

        pdf_path = None
        if pdf_url:
            dest = os.path.join(pdf_dir, case_id + ".pdf")
            try:
                time.sleep(beacg.DELAY)
                beacg.download(client, pdf_url, dest)
                pdf_path = dest
            except Exception as exc:
                print(f"[beacg fetch] {case_id}: download {exc}", file=sys.stderr)
                continue  # stay at 'new' for retry

        try:
            conn.execute(
                "UPDATE beacg_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[beacg fetch] {case_id}: db {exc}", file=sys.stderr)

    return len(rows)


def parse(conn):
    """
    For each status='fetched' row: extract text and classify.

    source_tier:
      'scanned' -- text below _SCANNED_FLOOR (image-only PDF)
      'pdf'     -- text length >= MIN_NARRATIVE
      'short'   -- some text, below MIN_NARRATIVE but >= _SCANNED_FLOOR
      'none'    -- no PDF / empty extraction

    Backfills the BEA file reference (into report_url), the first registration,
    and the cover-block fields (event date, aircraft, location, operator) from
    the report text only when the listing did not already supply them.

    Returns: number of rows processed.
    """
    rows = conn.execute(
        "SELECT case_id, pdf_path, report_url, registration, aircraft, location, "
        "operator, date_of_occurrence FROM beacg_reports WHERE status=?",
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

        bea_ref = row["report_url"] or beacg.find_bea_ref(full_text)
        registration = row["registration"] or beacg.find_registration(full_text)
        # Cover-block fields: backfill only when the listing left them empty.
        event_date = row["date_of_occurrence"] or beacg.extract_event_date(full_text)
        aircraft = row["aircraft"] or beacg.extract_aircraft(full_text)
        location = row["location"] or beacg.extract_location(full_text)
        operator = row["operator"] or beacg.extract_operator(full_text)

        conn.execute(
            "UPDATE beacg_reports "
            "SET narrative_text=?, source_tier=?, report_url=?, registration=?, "
            "date_of_occurrence=?, aircraft=?, location=?, operator=?, "
            "status=?, updated_at=? WHERE case_id=?",
            (narrative, tier, bea_ref, registration,
             event_date, aircraft, location, operator,
             db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()

    return len(rows)


def build(conn):
    """
    For each status='parsed' row: emit a beacg_accidents record or skip.

    Skip (status -> 'skipped') when narrative_text < _NARRATIVE_FLOOR chars
    (this covers every 'scanned' / 'none' row).

    report_type carries the BEA file reference when one was found, else the
    event_class.  Returns: number of rows built (not skipped).
    """
    rows = conn.execute(
        "SELECT case_id, event_class, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, pdf_url, report_url "
        "FROM beacg_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE beacg_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        source_url = row["pdf_url"]
        site_slug = text.make_site_slug(row["aircraft"], row["registration"], row["location"])
        report_type = row["report_url"] or row["event_class"]

        conn.execute(
            "INSERT OR REPLACE INTO beacg_accidents "
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
                "CG",
                narrative,
                None,
                source_url,
                report_type,
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE beacg_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    return built
