# aaiubg_ingest/pipeline.py
"""
discover → fetch → parse → build pipeline for Bulgaria AAIU (source key aaiubg).

Notes:
  discover(): fetches the AAIU index, walks every per-year sub-category page,
  parses each report row out of the Drupal node body, and INSERTs new case_ids
  into aaiubg_reports with full listing metadata.  Idempotent — existing
  case_ids are skipped.  All reports are the official English PDF (lang='en').

  fetch(): for each status='new' row that has a pdf_url: downloads the PDF
  (Referer header) and advances to 'fetched'.  Per-row try/except: a download
  failure keeps the row at 'new' for the next run (does NOT advance).

  parse(): extracts text from the PDF via pdftotext.  source_tier='pdf' when
  the text is >= MIN_NARRATIVE chars, 'short' when shorter but non-empty,
  'none' when empty.  These AAIU PDFs are bilingual (EN/FR or EN/BG) text-layer
  reports; the whole extracted text is stored — downstream P3 handles language.
  Scanned-image PDFs (extraction < ~500 chars) end up 'short'/'none' and are
  dropped by build()'s narrative floor.

  build(): emits aaiubg_accidents rows (country 'BG').  Rows whose
  narrative_text is shorter than _NARRATIVE_FLOOR (80 chars) are skipped.
"""
import os
import sys
import time

from . import aaiubg, db, text
from .pdf import extract_text, MIN_NARRATIVE

# 300 matches prod's NARRATIVE_MIN. It was 80 on the host, which
# admitted rows of 80-299 chars that prod can only ever render
# noindex. _common/tests/test_narrative_floor enforces this.
_NARRATIVE_FLOOR = 300  # chars; rows with less are treated as non-report / scanned


def discover(conn, client, full=False):
    """
    Walk the AAIU per-year pages and INSERT new case_ids into aaiubg_reports.

    full: accepted for API parity; the whole index is always walked, and
          per-case_id skip handles idempotency.

    All AAIU reports are official English PDFs → lang='en'.

    Returns: number of rows inserted.
    """
    index_resp = client.get(aaiubg.INDEX_URL)
    index_resp.raise_for_status()
    index_html = (
        index_resp.content.decode("utf-8", "replace")
        if isinstance(index_resp.content, bytes) else index_resp.content
    )

    year_urls = aaiubg.iter_year_urls(index_html)

    if not year_urls:

        # The index lists one URL per year. Zero of them means the

        # index markup changed; 28 rows from this source are already

        # in production, so it is not that the authority published

        # nothing. An empty individual YEAR is left unguarded on

        # purpose — a year with no accidents is ordinary.

        raise RuntimeError(

            "[aaiubg discover] the index yielded no year URLs. The markup"

            " has probably changed; refusing to report an empty run as"

            " success."

        )


    inserted = 0
    for year_url in year_urls:
        time.sleep(aaiubg.DELAY)
        try:
            year_resp = client.get(year_url)
            year_resp.raise_for_status()
            year_html = (
                year_resp.content.decode("utf-8", "replace")
                if isinstance(year_resp.content, bytes) else year_resp.content
            )
        except Exception as exc:
            print(f"[aaiubg discover] {year_url}: {exc}", file=sys.stderr)
            continue

        rows = aaiubg.parse_listing(year_html, year_url)
        for row in rows:
            case_id = row["case_id"]
            if conn.execute(
                "SELECT 1 FROM aaiubg_reports WHERE case_id=?", (case_id,)
            ).fetchone():
                continue  # already known

            pdf_url = row.get("pdf_url")
            lang = "en" if pdf_url else None

            ts = db.now_ms()
            conn.execute(
                "INSERT INTO aaiubg_reports "
                "(case_id, report_url, pdf_url, pdf_filename, title, event_class, "
                "aircraft, registration, date_of_occurrence, operator, lang, "
                "status, discovered_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    case_id,
                    year_url,
                    pdf_url,
                    row.get("pdf_filename"),
                    row.get("title"),
                    row.get("event_class"),
                    row.get("aircraft"),
                    row.get("registration"),
                    row.get("date_of_occurrence"),
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
    """
    For each status='new' row: download the PDF (if pdf_url is set) and
    advance to 'fetched'.

    Rows with no pdf_url are advanced to 'fetched' with pdf_path=None.

    Per-row try/except: a download failure keeps the row at 'new' for retry.
    The loop always continues to the next row regardless of errors.

    Returns: number of rows iterated (including failures).
    """
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, pdf_url FROM aaiubg_reports WHERE status=?",
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
                time.sleep(aaiubg.DELAY)
                aaiubg.download(client, pdf_url, dest)
                pdf_path = dest
            except Exception as exc:
                print(f"[aaiubg fetch] {case_id}: download {exc}", file=sys.stderr)
                # stay at 'new' for retry — do NOT advance
                continue

        try:
            conn.execute(
                "UPDATE aaiubg_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[aaiubg fetch] {case_id}: db {exc}", file=sys.stderr)

    return len(rows)


def parse(conn):
    """
    For each status='fetched' row: extract text from the PDF (if present).

    source_tier:
      'pdf'   — text length >= MIN_NARRATIVE (600 chars)
      'short' — text present but below threshold (incl. scanned-image PDFs)
      'none'  — no text at all (no PDF or empty extraction)

    Returns: number of rows processed.
    """
    rows = conn.execute(
        "SELECT case_id, pdf_path FROM aaiubg_reports WHERE status=?",
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
            tier = "short"
        else:
            narrative = ""
            tier = "none"

        conn.execute(
            "UPDATE aaiubg_reports "
            "SET narrative_text=?, source_tier=?, status=?, updated_at=? "
            "WHERE case_id=?",
            (narrative, tier, db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()

    return len(rows)


def build(conn):
    """
    For each status='parsed' row: emit an aaiubg_accidents record or skip.

    Skip criteria (status → 'skipped'):
      • narrative_text shorter than _NARRATIVE_FLOOR chars (scanned / empty).

    source_url: pdf_url if present, else report_url.

    Returns: number of rows built (not skipped).
    """
    rows = conn.execute(
        "SELECT case_id, event_class, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, pdf_url, report_url "
        "FROM aaiubg_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE aaiubg_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        source_url = row["pdf_url"] or row["report_url"]
        site_slug = text.make_site_slug(row["case_id"])

        conn.execute(
            "INSERT OR REPLACE INTO aaiubg_accidents "
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
                "BG",
                narrative,
                None,
                source_url,
                row["event_class"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE aaiubg_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    return built
