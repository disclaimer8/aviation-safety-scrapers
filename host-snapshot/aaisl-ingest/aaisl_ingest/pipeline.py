# aaisl_ingest/pipeline.py
"""
discover → fetch → parse → build pipeline for AAII Sri Lanka (CAA Sri Lanka).

Notes:
  discover(): fetches the single English listing page, parses report rows and
  INSERTs new intrinsic case_ids into aaisl_reports with full metadata.  The
  source is single-language: pdf_url = pdf_url_en, lang = 'en'.  Idempotent —
  existing case_ids are skipped.

  fetch(): downloads each status='new' report PDF and advances to 'fetched'.
  Per-row try/except: a download failure keeps the row at 'new' for retry.

  parse(): extracts PDF text via pdftotext.
    • If the report is actually a FOREIGN authority's report (e.g. the re-hosted
      TSIB / Singapore report), it is SKIPPED with source_tier='foreign' so it
      does not duplicate our existing `tsib` source.
    • Otherwise source_tier is 'pdf' (>= MIN_NARRATIVE), 'short' (some text), or
      'none' (no text).  Scanned-image PDFs that yield < ~500 chars of text are
      treated as 'scanned' and skipped at build time.

  build(): emits aaisl_accidents rows; rows with status='parsed' and a usable
  narrative are built.  Foreign-authority / scanned / too-short rows are skipped.
"""
import os
import sys
import time

from . import aaisl, db, text
from .pdf import extract_text, ocr_extract, MIN_NARRATIVE

_NARRATIVE_FLOOR = 80   # chars; below this is treated as a non-report event
_SCANNED_FLOOR = 500    # chars; below this a PDF is presumed scanned-image
OCR_LANG = "eng"        # AAII Sri Lanka reports are English; tesseract language for scanned PDFs


def discover(conn, client, full=False):
    """Fetch the listing page and INSERT new case_ids into aaisl_reports.

    full: accepted for API parity; the whole single listing page is always
          walked, and per-case_id skip handles idempotency.

    Returns: number of rows inserted.
    """
    resp = client.get(aaisl.INDEX_URL)
    resp.raise_for_status()
    index_html = (
        resp.content.decode("utf-8", "replace")
        if isinstance(resp.content, bytes) else resp.content
    )

    rows = aaisl.parse_listing(index_html)

    inserted = 0
    for row in rows:
        case_id = row["case_id"]
        if conn.execute(
            "SELECT 1 FROM aaisl_reports WHERE case_id=?", (case_id,)
        ).fetchone():
            continue  # already known

        pdf_url = row.get("pdf_url_en")
        lang = "en" if pdf_url else None

        ts = db.now_ms()
        conn.execute(
            "INSERT INTO aaisl_reports "
            "(case_id, report_url, pdf_url, pdf_url_es, pdf_url_en, "
            "title, event_class, aircraft, registration, date_of_occurrence, "
            "location, operator, lang, status, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                row.get("operator"),
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
    """Download status='new' PDFs and advance to 'fetched'.

    Per-row try/except: a download failure keeps the row at 'new' for retry.

    Returns: number of rows iterated (including failures).
    """
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, pdf_url FROM aaisl_reports WHERE status=?",
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
                time.sleep(aaisl.DELAY)
                aaisl.download(client, pdf_url, dest)
                pdf_path = dest
            except Exception as exc:
                print(f"[aaisl fetch] {case_id}: download {exc}", file=sys.stderr)
                continue  # stay at 'new' for retry — do NOT advance

        try:
            conn.execute(
                "UPDATE aaisl_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[aaisl fetch] {case_id}: db {exc}", file=sys.stderr)

    return len(rows)


def parse(conn, enable_ocr=True):
    """Extract text from each status='fetched' PDF.

    Image-only (scanned) PDFs have an empty/degenerate text layer; when
    pdftotext yields less than MIN_NARRATIVE we OCR the PDF (on OCR_REMOTE) and
    keep the OCR text if it recovered more — tier='ocr'. OCR that stays thin
    falls through to the 'scanned'/'short'/'none' tiers and is dropped by
    build() (quality self-filter).

    source_tier:
      'foreign' — a re-hosted FOREIGN authority report (e.g. TSIB/Singapore);
                  SKIPPED to avoid duplicating the `tsib` source.
      'ocr'     — recovered from a scanned PDF via OCR, length >= MIN_NARRATIVE
      'pdf'     — text length >= MIN_NARRATIVE (600 chars)
      'scanned' — some text but below _SCANNED_FLOOR (~500): presumed image scan
      'short'   — text between _SCANNED_FLOOR and MIN_NARRATIVE
      'none'    — no text at all

    Foreign and 'none' rows go straight to 'skipped'; the rest advance to
    'parsed' for build() to evaluate.

    enable_ocr: when False (CI / no OCR host), the OCR fallback is skipped.

    Returns: number of rows processed.
    """
    rows = conn.execute(
        "SELECT case_id, pdf_path FROM aaisl_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    for row in rows:
        pdf_path = row["pdf_path"]
        full_text = extract_text(pdf_path) if pdf_path else ""

        # TSIB-rehost / foreign-authority dedup: skip outright.
        if full_text and aaisl.is_foreign_authority(full_text):
            conn.execute(
                "UPDATE aaisl_reports "
                "SET narrative_text=?, source_tier=?, status=?, updated_at=? "
                "WHERE case_id=?",
                ("", "foreign", db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        # OCR fallback: thin-text (scanned/image-only) PDFs are re-read on
        # OCR_REMOTE; keep the OCR text only if it recovered more.
        ocr_recovered = False
        if len(full_text) < MIN_NARRATIVE and pdf_path and enable_ocr:
            ocr_text = ocr_extract(pdf_path, lang=OCR_LANG)
            if len(ocr_text) > len(full_text):
                full_text = ocr_text
                ocr_recovered = True

        if not full_text:
            narrative, tier, status = "", "none", db.STATUS_SKIPPED
        elif len(full_text) >= MIN_NARRATIVE:
            narrative, status = full_text, db.STATUS_PARSED
            tier = "ocr" if ocr_recovered else "pdf"
        elif len(full_text) < _SCANNED_FLOOR:
            narrative, tier, status = full_text, "scanned", db.STATUS_PARSED
        else:
            narrative, tier, status = full_text, "short", db.STATUS_PARSED

        conn.execute(
            "UPDATE aaisl_reports "
            "SET narrative_text=?, source_tier=?, status=?, updated_at=? "
            "WHERE case_id=?",
            (narrative, tier, status, db.now_ms(), row["case_id"]),
        )
        conn.commit()

    return len(rows)


def build(conn):
    """Emit aaisl_accidents rows from status='parsed' reports.

    Skip criteria (status → 'skipped'):
      • source_tier == 'scanned' (presumed image-only scan), or
      • narrative_text shorter than _NARRATIVE_FLOOR chars.

    Returns: number of rows built (not skipped).
    """
    rows = conn.execute(
        "SELECT case_id, event_class, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, source_tier, pdf_url, report_url "
        "FROM aaisl_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if row["source_tier"] == "scanned" or len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE aaisl_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        source_url = row["pdf_url"] or row["report_url"]
        site_slug = text.make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )

        conn.execute(
            "INSERT OR REPLACE INTO aaisl_accidents "
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
                "LK",
                narrative,
                None,
                source_url,
                row["event_class"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE aaisl_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    return built
