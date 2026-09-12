# aaicmv_ingest/pipeline.py
"""
discover -> fetch -> parse -> build pipeline for AAIC Maldives.

  discover(): fetches the single accidents-incidents listing page and INSERTs
  new case_ids into aaicmv_reports with listing metadata. Idempotent — existing
  case_ids are skipped. lang is always 'en' (Maldives publishes in English).

  fetch(): for each status='new' row downloads its PDF and advances to
  'fetched'. Per-row try/except: a download failure keeps the row at 'new' for
  the next run.

  parse(): extracts text via pdftotext. source_tier:
    'pdf'     — text >= MIN_NARRATIVE (600)
    'short'   — some text but below threshold
    'scanned' — almost no extractable text (< _SCANNED_FLOOR ~500) yet a PDF
                exists (image-only scan); skipped at build time
    'none'    — no PDF / no text

  build(): emits aaicmv_accidents rows; rows whose narrative_text is shorter
  than _NARRATIVE_FLOOR (80) — or tier 'scanned'/'none' — are skipped.
"""
import os
import sys
import time

from . import aaicmv, db, pdf, text
from .pdf import extract_text, MIN_NARRATIVE

# 300 matches prod's NARRATIVE_MIN. It was 80 on the host, which
# admitted rows of 80-299 chars that prod can only ever render
# noindex. _common/tests/test_narrative_floor enforces this.
_NARRATIVE_FLOOR = 300   # chars; below this the row is treated as a non-report
_SCANNED_FLOOR = 500    # chars; a PDF that yields less is an image-only scan
OCR_LANG = "eng"        # Maldives reports are English; tesseract lang for scans


def discover(conn, client, full=False):
    """Fetch the listing page and INSERT new case_ids. Returns rows inserted."""
    resp = client.get(aaicmv.INDEX_URL)
    resp.raise_for_status()
    index_html = (
        resp.content.decode("utf-8", "replace")
        if isinstance(resp.content, bytes) else resp.content
    )

    rows = aaicmv.parse_listing(index_html)


    if not rows:

        # 45 rows from this source are already in production, so an

        # empty listing is the markup changing — not the authority

        # publishing nothing. Returning 0 here is indistinguishable

        # from a clean run, which is how a dead scraper stays quiet.

        raise RuntimeError(

            "[aaicmv discover] listing parsed to zero rows. The markup has"

            " probably changed; refusing to report an empty run as success."

        )

    inserted = 0
    for row in rows:
        case_id = row["case_id"]
        if conn.execute(
            "SELECT 1 FROM aaicmv_reports WHERE case_id=?", (case_id,)
        ).fetchone():
            continue  # already known

        ts = db.now_ms()
        conn.execute(
            "INSERT INTO aaicmv_reports "
            "(case_id, report_url, pdf_url, pdf_url_en, "
            "title, event_class, aircraft, registration, date_of_occurrence, "
            "location, lang, status, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                case_id,
                aaicmv.INDEX_URL,
                row.get("pdf_url"),
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
    """Download PDFs for status='new' rows; advance to 'fetched'."""
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, pdf_url FROM aaicmv_reports WHERE status=?",
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
                time.sleep(aaicmv.DELAY)
                aaicmv.download(client, pdf_url, dest)
                pdf_path = dest
            except Exception as exc:
                print(f"[aaicmv fetch] {case_id}: download {exc}", file=sys.stderr)
                continue  # stay at 'new' for retry

        try:
            conn.execute(
                "UPDATE aaicmv_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[aaicmv fetch] {case_id}: db {exc}", file=sys.stderr)

    return len(rows)


def parse(conn, enable_ocr=True):
    """Extract PDF text for status='fetched' rows; set source_tier.

    Image-only (scanned) PDFs have an empty/degenerate text layer; when
    pdftotext yields less than MIN_NARRATIVE we OCR the PDF (on OCR_REMOTE when
    configured, else locally) and keep the OCR text if it recovered more. OCR
    text that reaches MIN_NARRATIVE is tier='ocr' and survives build(); OCR that
    stays thin falls through to 'scanned'/'short' and is dropped as before.

    enable_ocr: when False (CI / no OCR host) the OCR fallback is skipped.
    """
    rows = conn.execute(
        "SELECT case_id, pdf_path FROM aaicmv_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    for row in rows:
        pdf_path = row["pdf_path"]
        full_text = extract_text(pdf_path) if pdf_path else ""

        ocr_used = False
        if len(full_text) < MIN_NARRATIVE and pdf_path and enable_ocr:
            ocr_text = pdf.ocr_extract(pdf_path, lang=OCR_LANG)
            if len(ocr_text) > len(full_text):
                full_text = ocr_text
                ocr_used = True

        if len(full_text) >= MIN_NARRATIVE:
            narrative, tier = full_text, ("ocr" if ocr_used else "pdf")
        elif len(full_text) >= _SCANNED_FLOOR:
            narrative, tier = full_text, "short"
        elif pdf_path and len(full_text) < _SCANNED_FLOOR:
            # A PDF exists but yields almost no text -> image-only scan.
            narrative, tier = full_text, "scanned"
        elif full_text:
            narrative, tier = full_text, "short"
        else:
            narrative, tier = "", "none"

        conn.execute(
            "UPDATE aaicmv_reports "
            "SET narrative_text=?, source_tier=?, status=?, updated_at=? "
            "WHERE case_id=?",
            (narrative, tier, db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()

    return len(rows)


def build(conn):
    """Emit aaicmv_accidents rows. Skips scanned/none/too-short narratives."""
    rows = conn.execute(
        "SELECT case_id, event_class, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, source_tier, pdf_url, report_url "
        "FROM aaicmv_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        tier = row["source_tier"]
        if tier in ("scanned", "none") or len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE aaicmv_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        source_url = row["pdf_url"] or row["report_url"]
        site_slug = text.make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )

        conn.execute(
            "INSERT OR REPLACE INTO aaicmv_accidents "
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
                "MV",
                narrative,
                None,
                source_url,
                row["event_class"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE aaicmv_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    return built
