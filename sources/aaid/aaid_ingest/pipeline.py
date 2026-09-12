# aaid_ingest/pipeline.py
"""
discover → fetch → parse → build pipeline for Kenya AAID /final-reports.

Notes:
  discover(): GETs the single /final-reports table (pinned cert) and INSERTs
  new case_ids into aaid_reports with listing metadata.  case_id is intrinsic
  (registration + ISO event-date), so the operation is order-independent and
  idempotent — existing case_ids are skipped.  Source is English; pdf_url and
  lang reflect that.

  fetch(): for each status='new' row with a pdf_url, downloads the PDF over the
  pinned-cert client and advances to 'fetched'.  Rows with no pdf_url advance
  with pdf_path=None.  Per-row try/except: a download failure keeps the row at
  'new' for retry (does NOT advance).

  parse(): extracts text via pdftotext.  text >= MIN_NARRATIVE (600) →
  source_tier='pdf'.  If the text-layer is below MIN_NARRATIVE, an OCR fallback
  (ocr_extract, ocrmypdf+tesseract, lang='eng') is attempted: OCR text >=
  _NARRATIVE_FLOOR → source_tier='ocr' (buildable, but flagged as noisier than
  clean text-layer).  Otherwise short non-empty text → 'short'; nothing → 'none'.
  A truly blank/garbage scan stays 'none' and build() skips it.

  build(): emits aaid_accidents rows; rows with narrative_text shorter than
  _NARRATIVE_FLOOR (80 chars) are skipped (covers scanned reports). country=KE.
"""
import os
import sys
import time

from . import aaid, db, text
from .pdf import extract_text, ocr_extract, MIN_NARRATIVE

# 300 matches prod's NARRATIVE_MIN. It was 80 on the host, which
# admitted rows of 80-299 chars that prod can only ever render
# noindex. _common/tests/test_narrative_floor enforces this.
_NARRATIVE_FLOOR = 300  # chars; rows with less (e.g. scanned PDFs) are skipped


def discover(conn, client, full=False):
    """
    GET the AAID final-reports table and INSERT new case_ids into aaid_reports.

    full: accepted for API parity; the whole table is always walked and
          per-case_id skip handles idempotency.

    Returns: number of rows inserted.
    """
    index_resp = client.get(aaid.INDEX_URL)
    index_resp.raise_for_status()
    content = index_resp.content
    index_html = content.decode("utf-8", "replace") if isinstance(content, bytes) else content

    rows = aaid.parse_listing(index_html, aaid.INDEX_URL)


    if not rows:

        # 166 rows from this source are already in production, so an

        # empty listing is the markup changing — not the authority

        # publishing nothing. Returning 0 here is indistinguishable

        # from a clean run, which is how a dead scraper stays quiet.

        raise RuntimeError(

            "[aaid discover] listing parsed to zero rows. The markup has"

            " probably changed; refusing to report an empty run as success."

        )

    inserted = 0
    for row in rows:
        case_id = row["case_id"]
        if conn.execute(
            "SELECT 1 FROM aaid_reports WHERE case_id=?", (case_id,)
        ).fetchone():
            continue  # already known

        pdf_url = row.get("pdf_url")
        lang = row.get("lang") or ("en" if pdf_url else None)

        ts = db.now_ms()
        conn.execute(
            "INSERT INTO aaid_reports "
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
    For each status='new' row: download the PDF (if pdf_url set) and advance to
    'fetched'.  Rows with no pdf_url advance with pdf_path=None.

    Per-row try/except: a download failure keeps the row at 'new' for retry.

    Returns: number of rows iterated (including failures).
    """
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, pdf_url FROM aaid_reports WHERE status=?",
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
                time.sleep(aaid.DELAY)
                aaid.download(client, pdf_url, dest)
                pdf_path = dest
            except Exception as exc:
                print(f"[aaid fetch] {case_id}: download {exc}", file=sys.stderr)
                continue  # stay at 'new' for retry — do NOT advance

        try:
            conn.execute(
                "UPDATE aaid_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[aaid fetch] {case_id}: db {exc}", file=sys.stderr)

    return len(rows)


def parse(conn):
    """
    For each status='fetched' row: extract PDF text (if present).

    source_tier:
      'pdf'   — clean text-layer length >= MIN_NARRATIVE (600 chars)
      'short' — clean text-layer present but below MIN_NARRATIVE
      'ocr'   — text-layer was below MIN_NARRATIVE and an OCR fallback
                (ocr_extract, lang='eng') recovered text >= _NARRATIVE_FLOOR
                (these scanned/image-only reports are buildable, like 'pdf')
      'none'  — no usable text (no PDF, or both text-layer and OCR < floor)

    OCR fallback path: only image-only / degenerate-text-layer PDFs (text below
    MIN_NARRATIVE) are OCR'd — PDFs with a good text layer are never re-OCR'd.

    Returns: number of rows processed.
    """
    rows = conn.execute(
        "SELECT case_id, pdf_path FROM aaid_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    for row in rows:
        pdf_path = row["pdf_path"]
        full_text = extract_text(pdf_path) if pdf_path else ""

        if len(full_text) >= MIN_NARRATIVE:
            narrative, tier = full_text, "pdf"
        else:
            # text-layer below the scanned floor → try OCR fallback before
            # giving up.  Only PDFs that actually exist on disk are OCR'd.
            ocr_text = ocr_extract(pdf_path, "eng") if pdf_path else ""
            if len(ocr_text) >= _NARRATIVE_FLOOR:
                narrative, tier = ocr_text, "ocr"
            elif full_text:
                narrative, tier = full_text, "short"
            else:
                narrative, tier = "", "none"

        conn.execute(
            "UPDATE aaid_reports "
            "SET narrative_text=?, source_tier=?, status=?, updated_at=? "
            "WHERE case_id=?",
            (narrative, tier, db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()

    return len(rows)


def build(conn):
    """
    For each status='parsed' row: emit an aaid_accidents record or skip.

    Skip criteria (status → 'skipped'):
      • narrative_text shorter than _NARRATIVE_FLOOR chars (scanned/empty).

    source_url: pdf_url if present, else report_url.  country = 'KE'.

    Returns: number of rows built (not skipped).
    """
    rows = conn.execute(
        "SELECT case_id, event_class, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, pdf_url, report_url "
        "FROM aaid_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE aaid_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        source_url = row["pdf_url"] or row["report_url"]
        site_slug = text.make_site_slug(row["aircraft"], row["registration"], row["location"])

        conn.execute(
            "INSERT OR REPLACE INTO aaid_accidents "
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
                "KE",
                narrative,
                None,
                source_url,
                row["event_class"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE aaid_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    return built
