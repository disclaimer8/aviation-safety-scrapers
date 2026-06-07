# ansv_ingest/pipeline.py
"""
discover → fetch → parse → build pipeline for ANSV (Italy) WordPress listing.

Notes:
  discover(): walks every listing page from the ANSV index, inserts new
  case_ids into ansv_reports with full listing metadata.  Idempotent —
  existing case_ids are skipped.

  fetch(): for each status='new' row that has a pdf_url: downloads the PDF
  and advances to 'fetched'.  Rows without a pdf_url are also advanced to
  'fetched' with pdf_path=None so that parse() can handle them gracefully.
  Per-row try/except: a download failure keeps the row at 'new' for the
  next run (does NOT advance).

  parse(): extracts text from the PDF via pdftotext.  If text meets
  MIN_NARRATIVE (600 chars) → source_tier='pdf'.  If a PDF exists but yields
  tiny/no text (scanned image) → source_tier='scanned'.  No PDF at all →
  source_tier='none'.

  build(): emits ansv_accidents rows.  Rows whose narrative_text is shorter
  than _NARRATIVE_FLOOR (80 chars) are skipped.
"""
import os
import sys
import time

from . import ansv, db, text
from .pdf import extract_text, MIN_NARRATIVE

_NARRATIVE_FLOOR = 80  # chars; rows with less are treated as non-report events


def discover(conn, client, full=False):
    """
    Walk the ANSV listing pages and INSERT new case_ids into ansv_reports.

    Fetches each listing page (page 1 first to determine last_page, then
    pages 2..N), parses each article entry, fetches the report page to get
    the PDF URL, and inserts rows into ansv_reports.

    Idempotent: existing case_ids are skipped via INSERT OR IGNORE.
    full: accepted for API parity; has no extra effect (all pages always walked).

    Returns: number of new rows inserted.
    """
    inserted = 0

    # --- page 1: also determines pagination depth ---
    p1_html = client.get(ansv.LISTING_URL, headers={"User-Agent": ansv.UA}).text
    total_pages = ansv.last_page(p1_html)
    pages_html = {1: p1_html}

    # --- fetch remaining pages ---
    for n in range(2, total_pages + 1):
        time.sleep(ansv.DELAY)
        resp = client.get(ansv.page_url(n), headers={"User-Agent": ansv.UA})
        if resp.status_code != 200:
            print(f"[ansv discover] page {n}: HTTP {resp.status_code}", file=sys.stderr)
            continue
        pages_html[n] = resp.text

    # --- process entries ---
    for n, html in pages_html.items():
        entries = ansv.parse_listing(html)
        for entry in entries:
            report_url = entry["report_url"]

            # fetch report page for PDF URL
            time.sleep(ansv.DELAY)
            try:
                rresp = client.get(report_url, headers={"User-Agent": ansv.UA})
                rresp.raise_for_status()
                report_info = ansv.parse_report(rresp.text)
            except Exception as exc:
                print(f"[ansv discover] {report_url}: {exc}", file=sys.stderr)
                report_info = {"pdf_url": None, "title": entry.get("title")}

            pdf_url = report_info.get("pdf_url")
            title = report_info.get("title") or entry.get("title")

            case_id = ansv.make_case_id(
                entry.get("registration"),
                entry.get("date_of_occurrence"),
                report_url,
            )

            try:
                conn.execute(
                    "INSERT OR IGNORE INTO ansv_reports "
                    "(case_id, report_url, pdf_url, title, aircraft, registration, "
                    "date_of_occurrence, location, status, discovered_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        case_id,
                        report_url,
                        pdf_url,
                        title,
                        entry.get("aircraft"),
                        entry.get("registration"),
                        entry.get("date_of_occurrence"),
                        entry.get("location"),
                        db.STATUS_NEW,
                        db.now_ms(),
                        db.now_ms(),
                    ),
                )
                if conn.execute(
                    "SELECT changes()"
                ).fetchone()[0]:
                    inserted += 1
            except Exception as exc:
                print(f"[ansv discover] {case_id}: db {exc}", file=sys.stderr)

    conn.commit()
    return inserted


def fetch(conn, client, pdf_dir):
    """
    For each status='new' row: download the PDF (if pdf_url is set) and
    advance to 'fetched'.

    Returns: number of rows iterated (including failures).
    """
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, pdf_url FROM ansv_reports WHERE status=?",
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
                time.sleep(ansv.DELAY or 0)
                ansv.download(client, pdf_url, dest)
                pdf_path = dest
            except Exception as exc:
                print(f"[ansv fetch] {case_id}: download {exc}", file=sys.stderr)
                continue

        try:
            conn.execute(
                "UPDATE ansv_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[ansv fetch] {case_id}: db {exc}", file=sys.stderr)

    return len(rows)


def parse(conn):
    """
    For each status='fetched' row: extract text from the PDF (if present).

    Returns: number of rows processed.
    """
    rows = conn.execute(
        "SELECT case_id, pdf_path FROM ansv_reports WHERE status=?",
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
        elif pdf_path:
            # PDF exists but yielded tiny/no extractable text → scanned image
            narrative = full_text
            tier = "scanned"
        else:
            narrative = ""
            tier = "none"

        conn.execute(
            "UPDATE ansv_reports "
            "SET narrative_text=?, source_tier=?, status=?, updated_at=? "
            "WHERE case_id=?",
            (narrative, tier, db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()

    return len(rows)


def build(conn):
    """
    For each status='parsed' row: emit an ansv_accidents record or skip.

    Skip criteria (status → 'skipped'):
      • source_tier is not 'pdf' (scanned image PDFs, no extractable text).
      • narrative_text shorter than _NARRATIVE_FLOOR chars.

    source_url: pdf_url if present, else report_url.

    Returns: number of rows built (not skipped).
    """
    rows = conn.execute(
        "SELECT case_id, report_type, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, source_tier, pdf_url, report_url "
        "FROM ansv_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        source_tier = row["source_tier"] or ""
        # Skip scanned PDFs (no extractable text) and rows with too little narrative
        if source_tier != "pdf" or len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE ansv_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        source_url = row["pdf_url"] or row["report_url"]
        site_slug = text.make_site_slug(row["aircraft"], row["registration"], row["location"])

        conn.execute(
            "INSERT OR REPLACE INTO ansv_accidents "
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
                "IT",
                narrative,
                None,
                source_url,
                row["report_type"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE ansv_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    return built
