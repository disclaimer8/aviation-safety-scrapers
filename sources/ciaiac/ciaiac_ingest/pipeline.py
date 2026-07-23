# ciaiac_ingest/pipeline.py
"""
discover → fetch → parse → build pipeline for CIAIAC (Spain) per-year listing.

Notes:
  discover(): walks every year URL from the CIAIAC index page, inserts new
  case_ids into ciaiac_reports with full listing metadata.  EN-preference:
  pdf_url is set to pdf_url_en when available, else pdf_url_es; lang reflects
  the chosen language.  Idempotent — existing case_ids are skipped.

  fetch(): for each status='new' row that has a pdf_url: downloads the PDF
  and advances to 'fetched'.  Rows without a pdf_url (HTML-only provisional
  declarations) are also advanced to 'fetched' with pdf_path=None so that
  parse() can handle them gracefully.  Per-row try/except: a download failure
  keeps the row at 'new' for the next run (does NOT advance).

  parse(): extracts text from the PDF via pdftotext.  If text meets
  MIN_NARRATIVE (600 chars) → source_tier='pdf', else tier is 'short' when
  there is any text, 'none' when there is none.  Metadata was already stored
  at discover time from the rich listing H2, so no PDF header parse is
  required here.

  build(): emits ciaiac_accidents rows.  Rows whose narrative_text is shorter
  than _NARRATIVE_FLOOR (80 chars) are skipped.
"""
import os
import sys
import time

from . import ciaiac, db, text
from .pdf import extract_text, MIN_NARRATIVE

_NARRATIVE_FLOOR = 80  # chars; rows with less are treated as non-report events


def discover(conn, client, full=False):
    """
    Walk the CIAIAC per-year listing pages and INSERT new case_ids into
    ciaiac_reports.

    full: accepted for API parity; currently has no extra effect (the whole
          index is always walked; per-case_id skip handles idempotency).

    EN-preference: pdf_url = pdf_url_en or pdf_url_es; lang = 'en'/'es'/None.

    KNOWN rows are not simply skipped: CIAIAC lists an event as a PDF-less
    provisional declaration first and adds the report PDF to the SAME listing
    row later.  A known PDF-less stub ('new' or terminal 'skipped') whose
    listing row now carries a PDF gets its pdf fields updated and is re-queued
    to 'new' so this cycle's fetch() downloads it.  Costs nothing extra — the
    year pages are already fetched every walk.  (Found 2026-07-23: stubs froze
    at 'skipped' forever, losing every report published after first sight.)

    Returns: number of rows inserted (re-queued stubs are logged, not counted).
    """
    index_resp = client.get(ciaiac.INDEX_URL)
    index_resp.raise_for_status()
    index_html = index_resp.content.decode("utf-8", "replace") if isinstance(index_resp.content, bytes) else index_resp.content

    year_urls = ciaiac.iter_year_urls(index_html)

    inserted = 0
    for year_url in year_urls:
        time.sleep(ciaiac.DELAY)
        try:
            year_resp = client.get(year_url)
            year_resp.raise_for_status()
            year_html = year_resp.content.decode("utf-8", "replace") if isinstance(year_resp.content, bytes) else year_resp.content
        except Exception as exc:
            print(f"[ciaiac discover] {year_url}: {exc}", file=sys.stderr)
            continue

        rows = ciaiac.parse_listing(year_html, year_url)
        for row in rows:
            case_id = row["case_id"]
            existing = conn.execute(
                "SELECT status, pdf_url FROM ciaiac_reports WHERE case_id=?", (case_id,)
            ).fetchone()
            if existing:
                # Known PDF-less stub whose listing row has since gained a PDF:
                # store the links and put it back through fetch→parse→build.
                gained_en = row.get("pdf_url_en")
                gained_es = row.get("pdf_url_es")
                if (
                    existing["pdf_url"] is None
                    and (gained_en or gained_es)
                    and existing["status"] in (db.STATUS_NEW, db.STATUS_SKIPPED)
                ):
                    conn.execute(
                        "UPDATE ciaiac_reports SET pdf_url=?, pdf_url_en=?, pdf_url_es=?, "
                        "lang=?, status=?, updated_at=? WHERE case_id=?",
                        (
                            gained_en or gained_es,
                            gained_en,
                            gained_es,
                            "en" if gained_en else "es",
                            db.STATUS_NEW,
                            db.now_ms(),
                            case_id,
                        ),
                    )
                    print(f"[ciaiac discover] {case_id}: stub gained PDF, re-queued", file=sys.stderr)
                continue  # already known

            # EN-preference: prefer the EN PDF when available
            pdf_url_en = row.get("pdf_url_en")
            pdf_url_es = row.get("pdf_url_es")
            if pdf_url_en:
                pdf_url = pdf_url_en
                lang = "en"
            elif pdf_url_es:
                pdf_url = pdf_url_es
                lang = "es"
            else:
                pdf_url = None
                lang = None

            ts = db.now_ms()
            conn.execute(
                "INSERT INTO ciaiac_reports "
                "(case_id, report_url, pdf_url, pdf_url_es, pdf_url_en, "
                "title, event_class, aircraft, registration, date_of_occurrence, "
                "location, lang, status, discovered_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    case_id,
                    row.get("report_url"),
                    pdf_url,
                    pdf_url_es,
                    pdf_url_en,
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
    For each status='new' row: download the PDF (if pdf_url is set) and
    advance to 'fetched'.

    Rows with no pdf_url (HTML-only provisional declarations) are advanced to
    'fetched' with pdf_path=None; parse() will produce an empty narrative and
    build() will skip them.

    Per-row try/except: a download failure keeps the row at 'new' for retry.
    The loop always continues to the next row regardless of errors.

    Returns: number of rows iterated (including failures).
    """
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, pdf_url FROM ciaiac_reports WHERE status=?",
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
                time.sleep(ciaiac.DELAY)
                ciaiac.download(client, pdf_url, dest)
                pdf_path = dest
            except Exception as exc:
                print(f"[ciaiac fetch] {case_id}: download {exc}", file=sys.stderr)
                # stay at 'new' for retry — do NOT advance
                continue

        try:
            conn.execute(
                "UPDATE ciaiac_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[ciaiac fetch] {case_id}: db {exc}", file=sys.stderr)

    return len(rows)


def parse(conn):
    """
    For each status='fetched' row: extract text from the PDF (if present).

    source_tier:
      'pdf'   — text length >= MIN_NARRATIVE (600 chars)
      'short' — text present but below threshold
      'none'  — no text at all (no PDF or empty extraction)

    Metadata was already captured from the listing at discover time; no PDF
    header parse is performed here.

    Returns: number of rows processed.
    """
    rows = conn.execute(
        "SELECT case_id, pdf_path FROM ciaiac_reports WHERE status=?",
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
            tier = "short"
        else:
            narrative = ""
            tier = "none"

        conn.execute(
            "UPDATE ciaiac_reports "
            "SET narrative_text=?, source_tier=?, status=?, updated_at=? "
            "WHERE case_id=?",
            (narrative, tier, db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()

    return len(rows)


def build(conn):
    """
    For each status='parsed' row: emit a ciaiac_accidents record or skip.

    Skip criteria (status → 'skipped'):
      • narrative_text shorter than _NARRATIVE_FLOOR chars.

    source_url: pdf_url if present, else report_url.

    Returns: number of rows built (not skipped).
    """
    rows = conn.execute(
        "SELECT case_id, event_class, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, pdf_url, report_url "
        "FROM ciaiac_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE ciaiac_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        source_url = row["pdf_url"] or row["report_url"]
        site_slug = text.make_site_slug(row["aircraft"], row["registration"], row["location"])

        conn.execute(
            "INSERT OR REPLACE INTO ciaiac_accidents "
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
                "ES",
                narrative,
                None,
                source_url,
                row["event_class"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE ciaiac_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    return built
