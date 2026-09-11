# aaiahk_ingest/pipeline.py
"""
discover → fetch → parse → build pipeline for HK AAIA (single register page).

Notes:
  discover(): fetches the one AAIA register page, parses every occurrence row,
  and INSERTs new case_ids into aaiahk_reports with full listing metadata.
  Source is English-only, so lang is always 'en' when a PDF is present.
  Idempotent — existing case_ids are skipped.

  Dedup (prelim/interim → final):  the AAIA register lists all three report
  types (PLR / ITR / IVR) as separate download links on the SAME HTML row.
  parse_listing() returns the canonical case_id (IVR > ITR > PLR precedence)
  plus a superseded_codes list of all lower-priority codes on that row.  When
  discover() encounters a row whose canonical id is an IVR and it has
  superseded_codes (i.e. earlier ITR/PLR ids), it marks those codes in
  aaiahk_reports with superseded_by=<IVR case_id>.  build() then deletes any
  superseded rows from aaiahk_accidents, so no stale prelim/interim page
  survives after the final report is published.  Rows that have no final yet
  (ITR or PLR only) are left untouched and continue to produce accident pages.

  fetch(): for each status='new' row that has a pdf_url: downloads the PDF and
  advances to 'fetched'. Rows with no pdf_url advance to 'fetched' with
  pdf_path=None. Per-row try/except: a download failure keeps the row at 'new'
  for the next run (does NOT advance).

  parse(): extracts text from the PDF via pdftotext.  Scanned-aware — text of
  MIN_NARRATIVE (600 chars) or more → source_tier='pdf'; some text below that
  (likely a scanned/empty-layer report) → 'scanned'; no text at all → 'none'.
  Metadata was already captured at discover time from the rich register row.

  build(): emits aaiahk_accidents rows.  Rows whose narrative_text is shorter
  than _NARRATIVE_FLOOR (80 chars) are skipped.  country='HK'.
  After building new rows, deletes any aaiahk_accidents rows that are now
  superseded (i.e. their aaiahk_reports row has superseded_by set), so stale
  prelim/interim pages are removed from the accident table each build cycle.
"""
import os
import sys
import time

from . import aaiahk, db, text
from .pdf import extract_text, MIN_NARRATIVE

_NARRATIVE_FLOOR = 80  # chars; rows with less are treated as non-report events


def discover(conn, client, full=False):
    """
    Fetch the AAIA register page and INSERT new case_ids into aaiahk_reports.

    full: accepted for API parity; has no extra effect (the single register
          page is always walked; per-case_id skip handles idempotency).

    Returns: number of rows inserted.
    """
    index_resp = client.get(aaiahk.INDEX_URL)
    index_resp.raise_for_status()
    index_html = (
        index_resp.content.decode("utf-8", "replace")
        if isinstance(index_resp.content, bytes) else index_resp.content
    )

    rows = aaiahk.parse_listing(index_html, aaiahk.INDEX_URL)

    inserted = 0
    for row in rows:
        case_id = row["case_id"]

        # ── Dedup: mark superseded ITR/PLR rows before inserting the new IVR ──
        # Any lower-priority code that appeared on the same listing row is now
        # superseded by this case_id.  We write superseded_by regardless of
        # whether the superseding row is new or already known; this handles the
        # case where the IVR was already in the DB from a previous run.
        for sup_code in row.get("superseded_codes", []):
            conn.execute(
                "UPDATE aaiahk_reports SET superseded_by=?, updated_at=? "
                "WHERE case_id=? AND (superseded_by IS NULL OR superseded_by != ?)",
                (case_id, db.now_ms(), sup_code, case_id),
            )
        if row.get("superseded_codes"):
            conn.commit()

        if conn.execute(
            "SELECT 1 FROM aaiahk_reports WHERE case_id=?", (case_id,)
        ).fetchone():
            continue  # already known

        pdf_url = row.get("pdf_url")
        lang = "en" if pdf_url else None

        ts = db.now_ms()
        conn.execute(
            "INSERT INTO aaiahk_reports "
            "(case_id, report_url, pdf_url, pdf_url_es, pdf_url_en, "
            "title, event_class, aircraft, registration, date_of_occurrence, "
            "location, lang, status, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                case_id,
                row.get("report_url"),
                pdf_url,
                row.get("pdf_url_es"),
                row.get("pdf_url_en"),
                row.get("title"),
                row.get("event_class"),
                row.get("aircraft"),
                row.get("registration"),
                row.get("date_of_occurrence"),
                row.get("location"),
                lang,
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
    For each status='new' row: download the PDF (if pdf_url is set) and advance
    to 'fetched'.

    Rows with no pdf_url advance to 'fetched' with pdf_path=None; parse() will
    produce an empty narrative and build() will skip them.

    Per-row try/except: a download failure keeps the row at 'new' for retry.

    Returns: number of rows iterated (including failures).
    """
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, pdf_url FROM aaiahk_reports WHERE status=?",
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
                time.sleep(aaiahk.DELAY)
                aaiahk.download(client, pdf_url, dest)
                pdf_path = dest
            except Exception as exc:
                print(f"[aaiahk fetch] {case_id}: download {exc}", file=sys.stderr)
                # stay at 'new' for retry — do NOT advance
                continue

        try:
            conn.execute(
                "UPDATE aaiahk_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[aaiahk fetch] {case_id}: db {exc}", file=sys.stderr)

    return len(rows)


def parse(conn):
    """
    For each status='fetched' row: extract text from the PDF (if present).

    source_tier:
      'pdf'     — text length >= MIN_NARRATIVE (600 chars)
      'scanned' — text present but below threshold (likely scanned/empty-layer)
      'none'    — no text at all (no PDF or empty extraction)

    Returns: number of rows processed.
    """
    rows = conn.execute(
        "SELECT case_id, pdf_path FROM aaiahk_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    for row in rows:
        pdf_path = row["pdf_path"]
        full_text = extract_text(pdf_path) if pdf_path else ""

        if len(full_text) >= MIN_NARRATIVE:
            narrative = full_text
            tier = "pdf"
        elif full_text:
            narrative = full_text
            tier = "scanned"
        else:
            narrative = ""
            tier = "none"

        # Back-fill registration from PDF body (PDF carries it, listing does not).
        # Stays None for paragliders / no-reg events. Preserves existing
        # registration if extraction returns None — listing-only override would
        # have happened at discover() time.
        reg = text.extract_registration(narrative)
        if reg:
            conn.execute(
                "UPDATE aaiahk_reports "
                "SET narrative_text=?, source_tier=?, registration=?, status=?, updated_at=? "
                "WHERE case_id=?",
                (narrative, tier, reg, db.STATUS_PARSED, db.now_ms(), row["case_id"]),
            )
        else:
            conn.execute(
                "UPDATE aaiahk_reports "
                "SET narrative_text=?, source_tier=?, status=?, updated_at=? "
                "WHERE case_id=?",
                (narrative, tier, db.STATUS_PARSED, db.now_ms(), row["case_id"]),
            )
        conn.commit()

    return len(rows)


def build(conn):
    """
    For each status='parsed' row: emit an aaiahk_accidents record or skip.

    Skip criteria (status → 'skipped'):
      • narrative_text shorter than _NARRATIVE_FLOOR chars.

    source_url: pdf_url if present, else report_url.

    After building new rows, removes any aaiahk_accidents rows that have been
    superseded (their aaiahk_reports.superseded_by IS NOT NULL), so stale
    prelim/interim pages are cleaned out each build cycle.

    Returns: number of rows built (not skipped).
    """
    rows = conn.execute(
        "SELECT case_id, event_class, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, pdf_url, report_url "
        "FROM aaiahk_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE aaiahk_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        source_url = row["pdf_url"] or row["report_url"]
        site_slug = text.make_site_slug(row["aircraft"], row["registration"], row["location"])

        conn.execute(
            "INSERT OR REPLACE INTO aaiahk_accidents "
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
                "HK",
                narrative,
                None,
                source_url,
                row["event_class"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE aaiahk_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    # ── Dedup: purge stale prelim/interim rows from aaiahk_accidents ──────
    # Any aaiahk_reports row with superseded_by set has been eclipsed by a
    # final IVR report.  Remove its accident page so the final row is the only
    # page for that occurrence.
    superseded = conn.execute(
        "SELECT case_id FROM aaiahk_reports WHERE superseded_by IS NOT NULL"
    ).fetchall()
    purged = 0
    for sup_row in superseded:
        sup_id = sup_row[0]
        deleted = conn.execute(
            "DELETE FROM aaiahk_accidents WHERE case_id=?", (sup_id,)
        ).rowcount
        if deleted:
            purged += 1
            print(f"[aaiahk build] purged superseded accident page: {sup_id}", file=sys.stderr)
    if purged:
        conn.commit()

    return built
