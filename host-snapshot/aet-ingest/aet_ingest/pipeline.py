# aet_ingest/pipeline.py
"""
discover -> fetch -> parse -> build pipeline for Luxembourg AET (single page).

discover(): GET the one aviation-civile.html listing page, parse the kept
  (final + historical) PDF rows and INSERT new case_ids (intrinsic
  'aet-<filename-slug>') into aet_reports with the PDF url, title and the
  per-document language (filename-detected).  Foreign-authority docs are
  recorded with event_class='Foreign-authority report'.  Idempotent.

fetch(): for each status='new' row, download the PDF (content/dam URLs are
  normalised to the working dam-assets twin; spaces percent-encoded) and
  advance to 'fetched'.  Per-row try/except: a failure keeps the row at 'new'.

parse(): extract text via pdftotext.  Scanned/empty-aware:
    text length <  _SCANNED_FLOOR (500) -> tier 'scanned' / 'none'
    text length >= MIN_NARRATIVE (600)  -> tier 'pdf'
    otherwise                           -> tier 'short'
  Harvests the registration (text first, then filename) and the cover-block
  fields (event date, aircraft, location, operator) + filename-date fallback.
  Refines lang with a text heuristic when the filename gave no clear signal.

build(): emit aet_accidents rows (country 'LU').  Rows whose narrative is
  shorter than _NARRATIVE_FLOOR (80) -- which includes 'scanned'/'none' rows
  (e.g. the broken empty fokker source) -- are skipped.  report_type carries
  the 'foreign-authority' marker for foreign-issued reports.
"""
import os
import sys
import time

from . import aet, db, text
from .pdf import extract_text, MIN_NARRATIVE

_NARRATIVE_FLOOR = 80   # chars; rows with less are non-report events
_SCANNED_FLOOR = 500    # chars; below this an image-only / empty PDF is assumed


def discover(conn, client, full=False):
    """
    GET the AET index page and INSERT new case_ids into aet_reports.

    full: accepted for API parity (the whole single page is always walked).
    lang is per-document (filename-detected).  Returns rows inserted.
    """
    resp = client.get(aet.INDEX_URL)
    resp.raise_for_status()
    index_html = (
        resp.content.decode("utf-8", "replace")
        if isinstance(resp.content, bytes) else resp.text
    )

    rows = aet.parse_listing(index_html)

    inserted = 0
    for row in rows:
        case_id = row["case_id"]
        if conn.execute(
            "SELECT 1 FROM aet_reports WHERE case_id=?", (case_id,)
        ).fetchone():
            continue  # already known

        event_class = (
            "Foreign-authority report" if row.get("foreign") else "Accident"
        )
        ts = db.now_ms()
        conn.execute(
            "INSERT INTO aet_reports "
            "(case_id, pdf_url, title, event_class, lang, status, "
            "discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                case_id,
                row.get("pdf_url"),
                row.get("title"),
                event_class,
                row.get("lang") or "fr",
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
        "SELECT case_id, pdf_url FROM aet_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()

    for row in rows:
        case_id = row["case_id"]
        pdf_url = row["pdf_url"]

        pdf_path = None
        if pdf_url:
            dest = os.path.join(pdf_dir, case_id + ".pdf")
            try:
                time.sleep(aet.DELAY)
                aet.download(client, pdf_url, dest)
                pdf_path = dest
            except Exception as exc:
                print(f"[aet fetch] {case_id}: download {exc}", file=sys.stderr)
                continue  # stay at 'new' for retry

        try:
            conn.execute(
                "UPDATE aet_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[aet fetch] {case_id}: db {exc}", file=sys.stderr)

    return len(rows)


def parse(conn):
    """
    For each status='fetched' row: extract text and classify.

    source_tier:
      'scanned' -- text below _SCANNED_FLOOR (image-only PDF)
      'pdf'     -- text length >= MIN_NARRATIVE
      'short'   -- some text, below MIN_NARRATIVE but >= _SCANNED_FLOOR
      'none'    -- no PDF / empty extraction (e.g. broken empty source file)

    Harvests the registration (text then filename), the cover-block fields
    (event date, aircraft, location, operator) with a filename-date fallback,
    and refines lang from the text when the filename was ambiguous.

    Returns: number of rows processed.
    """
    rows = conn.execute(
        "SELECT case_id, pdf_url, pdf_path, lang, registration, aircraft, "
        "location, operator, date_of_occurrence "
        "FROM aet_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    for row in rows:
        pdf_path = row["pdf_path"]
        pdf_url = row["pdf_url"]
        full_text = extract_text(pdf_path) if pdf_path else ""

        if not full_text:
            narrative, tier = "", "none"
        elif len(full_text) < _SCANNED_FLOOR:
            narrative, tier = full_text, "scanned"
        elif len(full_text) >= MIN_NARRATIVE:
            narrative, tier = full_text, "pdf"
        else:
            narrative, tier = full_text, "short"

        registration = (
            row["registration"]
            or aet.find_registration(full_text)
            or aet.registration_from_filename(pdf_url or "")
        )
        event_date = (
            row["date_of_occurrence"]
            or aet.extract_event_date(full_text)
            or aet.date_from_filename(pdf_url or "")
        )
        aircraft = row["aircraft"] or aet.extract_aircraft(full_text)
        location = row["location"] or aet.extract_location(full_text)
        operator = row["operator"] or aet.extract_operator(full_text)
        # refine lang: recompute from filename+text now that full text is
        # available; detect_lang prioritises the filename suffix then lets the
        # text heuristic break ties.
        lang = aet.detect_lang(pdf_url or "", full_text)

        conn.execute(
            "UPDATE aet_reports "
            "SET narrative_text=?, source_tier=?, lang=?, registration=?, "
            "date_of_occurrence=?, aircraft=?, location=?, operator=?, "
            "status=?, updated_at=? WHERE case_id=?",
            (narrative, tier, lang, registration,
             event_date, aircraft, location, operator,
             db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()

    return len(rows)


def build(conn):
    """
    For each status='parsed' row: emit an aet_accidents record or skip.

    Skip (status -> 'skipped') when narrative_text < _NARRATIVE_FLOOR chars
    (covers every 'scanned' / 'none' row, e.g. the broken empty source).

    report_type carries the 'foreign-authority' marker for reports issued by a
    foreign authority (event_class='Foreign-authority report'); otherwise the
    event_class.  Returns: number of rows built (not skipped).
    """
    rows = conn.execute(
        "SELECT case_id, event_class, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, pdf_url "
        "FROM aet_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE aet_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        source_url = row["pdf_url"]
        site_slug = text.make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )
        if row["event_class"] == "Foreign-authority report":
            report_type = aet.FOREIGN_AUTHORITY
        else:
            report_type = row["event_class"]

        conn.execute(
            "INSERT OR REPLACE INTO aet_accidents "
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
                "LU",
                narrative,
                None,
                source_url,
                report_type,
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE aet_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    return built
