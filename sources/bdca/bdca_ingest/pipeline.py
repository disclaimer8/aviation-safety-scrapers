# bdca_ingest/pipeline.py
"""discover → fetch → parse → build pipeline for Belize DCA / BAAI.

discover(): GET the single PhocaDownload accident-reports page, parse every
  report row, INSERT new intrinsic case_ids into bdca_reports. lang is always
  'en' (English source). Idempotent — existing case_ids are skipped.

fetch(): for each status='new' row with a pdf_url, download the PDF (Referer
  required) and advance to 'fetched'. Per-row try/except: a download failure
  keeps the row at 'new' for the next run.

parse(): extract text via pdftotext.
  'pdf'     — text >= MIN_NARRATIVE (600 chars)
  'short'   — text present, between SCANNED_FLOOR and MIN_NARRATIVE
  'scanned' — text below SCANNED_FLOOR (image-only / scanned report, no usable
              text layer) → skipped at build
  'none'    — no PDF / empty extraction

build(): emit bdca_accidents rows. Rows whose narrative is shorter than
  _NARRATIVE_FLOOR (80 chars) — which includes every 'scanned'/'none' row —
  are skipped (status='skipped'). country is always 'BZ'.
"""
import os
import sys
import time

from . import bdca, db, text
from .pdf import extract_text, MIN_NARRATIVE, SCANNED_FLOOR

# 300 matches prod's NARRATIVE_MIN. It was 80 on the host, which
# admitted rows of 80-299 chars that prod can only ever render
# noindex. _common/tests/test_narrative_floor enforces this.
_NARRATIVE_FLOOR = 300  # chars; rows with less are treated as non-report events


def discover(conn, client, full=False):
    """Walk the single accident-reports listing and INSERT new case_ids.

    full: accepted for API parity (no extra effect; the whole listing is
          always parsed and per-case_id skip handles idempotency).

    Returns: number of rows inserted.
    """
    resp = client.get(bdca.INDEX_URL)
    resp.raise_for_status()
    html = (
        resp.content.decode("utf-8", "replace")
        if isinstance(resp.content, bytes)
        else resp.content
    )

    rows = bdca.parse_listing(html)

    inserted = 0
    for row in rows:
        case_id = row["case_id"]
        if conn.execute(
            "SELECT 1 FROM bdca_reports WHERE case_id=?", (case_id,)
        ).fetchone():
            continue  # already known

        ts = db.now_ms()
        conn.execute(
            "INSERT INTO bdca_reports "
            "(case_id, report_url, pdf_url, title, event_class, aircraft, "
            "registration, date_of_occurrence, location, lang, status, "
            "discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                case_id,
                row.get("report_url"),
                row.get("pdf_url"),
                row.get("title"),
                row.get("event_class"),
                row.get("aircraft"),
                row.get("registration"),
                row.get("date_of_occurrence"),
                row.get("location"),
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
    """Download the PDF for each status='new' row and advance to 'fetched'.

    Rows with no pdf_url advance with pdf_path=None. A download failure keeps
    the row at 'new' for retry. Per-row isolation: the loop always continues.

    Returns: number of rows iterated (including failures).
    """
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, pdf_url FROM bdca_reports WHERE status=?",
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
                time.sleep(bdca.DELAY)
                bdca.download(client, pdf_url, dest)
                pdf_path = dest
            except Exception as exc:
                print(f"[bdca fetch] {case_id}: download {exc}", file=sys.stderr)
                # stay at 'new' for retry — do NOT advance
                continue

        try:
            conn.execute(
                "UPDATE bdca_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[bdca fetch] {case_id}: db {exc}", file=sys.stderr)

    return len(rows)


def parse(conn):
    """Extract text from each status='fetched' row's PDF.

    source_tier:
      'pdf'     — len >= MIN_NARRATIVE
      'short'   — SCANNED_FLOOR <= len < MIN_NARRATIVE
      'scanned' — 0 < len < SCANNED_FLOOR (image-only report, no text layer)
      'none'    — no PDF / empty extraction

    Returns: number of rows processed.
    """
    rows = conn.execute(
        "SELECT case_id, pdf_path FROM bdca_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    for row in rows:
        pdf_path = row["pdf_path"]
        full_text = extract_text(pdf_path) if pdf_path else ""

        if len(full_text) >= MIN_NARRATIVE:
            narrative, tier = full_text, "pdf"
        elif len(full_text) >= SCANNED_FLOOR:
            narrative, tier = full_text, "short"
        elif full_text:
            narrative, tier = full_text, "scanned"
        else:
            narrative, tier = "", "none"

        conn.execute(
            "UPDATE bdca_reports "
            "SET narrative_text=?, source_tier=?, status=?, updated_at=? "
            "WHERE case_id=?",
            (narrative, tier, db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()

    return len(rows)


def build(conn):
    """Emit a bdca_accidents record for each buildable status='parsed' row.

    Skip criteria (status → 'skipped'):
      • narrative_text shorter than _NARRATIVE_FLOOR chars (covers every
        'scanned'/'none' row as well as short fragments).

    source_url: pdf_url if present, else report_url. country='BZ'.

    Returns: number of rows built (not skipped).
    """
    rows = conn.execute(
        "SELECT case_id, event_class, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, pdf_url, report_url "
        "FROM bdca_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE bdca_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        source_url = row["pdf_url"] or row["report_url"]
        site_slug = text.make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )

        conn.execute(
            "INSERT OR REPLACE INTO bdca_accidents "
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
                "BZ",
                narrative,
                None,
                source_url,
                row["event_class"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE bdca_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    return built
