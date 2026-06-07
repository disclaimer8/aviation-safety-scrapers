# ciaiauy_ingest/pipeline.py
"""
discover -> fetch -> parse -> build pipeline for CIAIA (Uruguay).

discover(): crawls the fixed SEED_PATHS listing pages, unions every distinct PDF
  anchor, and INSERTs new rows into ciaiauy_reports.  case_id is built from the
  Caso number (caso-NNN) when present, else the registration slug, with a
  collision suffix to guarantee uniqueness across the whole source.  The same
  pdf_url discovered twice (it appears on more than one listing page) is inserted
  only once.  Idempotent — already-known pdf_urls are skipped.

fetch(): for each status='new' row downloads the PDF and advances to 'fetched'.
  Per-row try/except: a download failure keeps the row at 'new' for retry.

parse(): extracts text via pdftotext.
  source_tier:
    'pdf'     text length >= MIN_NARRATIVE
    'scanned' 0 < text length <= SCANNED_MAX  (image-only / no usable text layer)
    'short'   SCANNED_MAX < text length < MIN_NARRATIVE
    'none'    no text at all
  Narrative is kept in Spanish (EN translation is downstream).

build(): emits ciaiauy_accidents rows.  Rows with source_tier='scanned' or a
  narrative shorter than _NARRATIVE_FLOOR are skipped (no usable narrative).
"""
import os
import sys
import time

from . import ciaiauy, db, text
from .pdf import extract_text, MIN_NARRATIVE, SCANNED_MAX

_NARRATIVE_FLOOR = 80  # chars; rows with less are treated as non-report events


def discover(conn, client, full=False):
    """
    Crawl SEED_PATHS and INSERT new reports into ciaiauy_reports.

    full: accepted for API parity; the whole seed set is always walked and
          per-pdf_url skip provides idempotency.

    Returns: number of rows inserted.
    """
    # Build the set of already-assigned case_ids and known pdf_urls so we can
    # both skip duplicates and avoid case_id collisions across pages/runs.
    taken = {r["case_id"] for r in conn.execute("SELECT case_id FROM ciaiauy_reports")}
    known_pdfs = {
        r["pdf_url"]
        for r in conn.execute("SELECT pdf_url FROM ciaiauy_reports WHERE pdf_url IS NOT NULL")
    }

    inserted = 0
    for path in ciaiauy.SEED_PATHS:
        url = ciaiauy.BASE + path
        time.sleep(ciaiauy.DELAY)
        try:
            resp = client.get(url)
            resp.raise_for_status()
            page_html = (
                resp.content.decode("utf-8", "replace")
                if isinstance(resp.content, bytes)
                else resp.content
            )
        except Exception as exc:
            print(f"[ciaiauy discover] {url}: {exc}", file=sys.stderr)
            continue

        for row in ciaiauy.parse_listing(page_html):
            pdf_url = row["pdf_url"]
            if pdf_url in known_pdfs:
                continue  # already discovered (possibly on another page)
            known_pdfs.add(pdf_url)

            case_id = ciaiauy.make_case_id(
                row.get("caso"), row.get("registration"), taken=taken
            )
            taken.add(case_id)

            ts = db.now_ms()
            conn.execute(
                "INSERT INTO ciaiauy_reports "
                "(case_id, report_url, pdf_url, title, event_class, registration, "
                "date_of_occurrence, lang, status, discovered_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    case_id,
                    pdf_url,            # report_url == pdf_url (no separate page)
                    pdf_url,
                    row.get("title"),
                    row.get("event_class"),
                    row.get("registration"),
                    row.get("date_of_occurrence"),
                    "es",
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
    Download the PDF for each status='new' row and advance to 'fetched'.

    Per-row try/except: a download failure keeps the row at 'new' for retry.
    Returns: number of rows iterated (including failures).
    """
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, pdf_url FROM ciaiauy_reports WHERE status=?",
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
                time.sleep(ciaiauy.DELAY)
                ciaiauy.download(client, pdf_url, dest)
                pdf_path = dest
            except Exception as exc:
                print(f"[ciaiauy fetch] {case_id}: download {exc}", file=sys.stderr)
                continue  # stay at 'new' for retry — do NOT advance

        try:
            conn.execute(
                "UPDATE ciaiauy_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[ciaiauy fetch] {case_id}: db {exc}", file=sys.stderr)

    return len(rows)


def parse(conn):
    """
    Extract text from each status='fetched' PDF; tier it (scanned-aware).

    Returns: number of rows processed.
    """
    rows = conn.execute(
        "SELECT case_id, pdf_path FROM ciaiauy_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    for row in rows:
        pdf_path = row["pdf_path"]
        full_text = extract_text(pdf_path) if pdf_path else ""

        n = len(full_text)
        if n >= MIN_NARRATIVE:
            narrative, tier = full_text, "pdf"
        elif 0 < n <= SCANNED_MAX:
            narrative, tier = full_text, "scanned"
        elif n > 0:
            narrative, tier = full_text, "short"
        else:
            narrative, tier = "", "none"

        conn.execute(
            "UPDATE ciaiauy_reports "
            "SET narrative_text=?, source_tier=?, status=?, updated_at=? "
            "WHERE case_id=?",
            (narrative, tier, db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()

    return len(rows)


def build(conn):
    """
    Emit a ciaiauy_accidents record for each buildable status='parsed' row.

    Skip (status -> 'skipped') when source_tier='scanned' or the narrative is
    shorter than _NARRATIVE_FLOOR chars.

    Returns: number of rows built (not skipped).
    """
    rows = conn.execute(
        "SELECT case_id, event_class, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, source_tier, pdf_url, report_url "
        "FROM ciaiauy_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if row["source_tier"] == "scanned" or len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE ciaiauy_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        source_url = row["pdf_url"] or row["report_url"]
        site_slug = text.make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )

        conn.execute(
            "INSERT OR REPLACE INTO ciaiauy_accidents "
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
                "UY",
                narrative,
                None,
                source_url,
                row["event_class"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE ciaiauy_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    return built
