"""discover -> fetch -> parse -> build pipeline for Mongolia AAIB.

Key adaptation vs the CIAIAC template: the Mongolia report PDFs are SCANNED
IMAGES with no text layer, so pdftotext returns nothing.  parse() therefore
records source_tier='scanned' when a PDF is present but yields no text, and
build() falls back to the rich listing TITLE as the narrative so the
occurrence is still projected (the title carries date / aircraft / registration
/ event class).  P3 (OCR / LLM) enriches downstream.
"""
import os
import sys
import time

from . import aaibmn, db, text
from .pdf import extract_text, MIN_NARRATIVE

_NARRATIVE_FLOOR = 30  # chars; listing titles are short but meaningful


def discover(conn, client, full=False):
    """Walk the EN report-category pages and INSERT new case_ids.

    Idempotent — existing case_ids are skipped. Returns rows inserted.
    """
    inserted = 0
    for page_url, page_html in aaibmn.iter_listing_pages(client):
        try:
            rows = aaibmn.parse_listing(page_html)
        except Exception as exc:
            print(f"[aaibmn discover] {page_url}: parse {exc}", file=sys.stderr)
            continue

        for row in rows:
            case_id = row["case_id"]
            if conn.execute(
                "SELECT 1 FROM aaibmn_reports WHERE case_id=?", (case_id,)
            ).fetchone():
                continue

            ts = db.now_ms()
            conn.execute(
                "INSERT INTO aaibmn_reports "
                "(case_id, report_url, pdf_url, pdf_url_es, pdf_url_en, "
                "title, event_class, aircraft, registration, date_of_occurrence, "
                "location, lang, status, discovered_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    case_id,
                    row.get("report_url"),
                    row.get("pdf_url"),
                    row.get("pdf_url_es"),
                    row.get("pdf_url_en"),
                    row.get("title"),
                    row.get("event_class"),
                    row.get("aircraft"),
                    row.get("registration"),
                    row.get("date_of_occurrence"),
                    row.get("location"),
                    row.get("lang"),
                    db.STATUS_NEW,
                    ts,
                    ts,
                ),
            )
            inserted += 1
        conn.commit()
    return inserted


def fetch(conn, client, pdf_dir):
    """Download the PDF for each status='new' row; advance to 'fetched'.

    Rows with no pdf_url advance with pdf_path=None. Download failure keeps the
    row at 'new' for retry. Returns rows iterated.
    """
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, pdf_url FROM aaibmn_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()

    for row in rows:
        case_id = row["case_id"]
        pdf_url = row["pdf_url"]

        pdf_path = None
        if pdf_url:
            safe = case_id.replace("/", "_").replace(" ", "_")
            dest = os.path.join(pdf_dir, safe + ".pdf")
            try:
                time.sleep(aaibmn.DELAY)
                aaibmn.download(client, pdf_url, dest)
                pdf_path = dest
            except Exception as exc:
                print(f"[aaibmn fetch] {case_id}: download {exc}", file=sys.stderr)
                continue  # stay 'new'

        try:
            conn.execute(
                "UPDATE aaibmn_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[aaibmn fetch] {case_id}: db {exc}", file=sys.stderr)

    return len(rows)


def parse(conn):
    """Extract text from each fetched PDF.

    source_tier:
      'pdf'     — text >= MIN_NARRATIVE
      'short'   — some text, below threshold
      'scanned' — PDF present but no extractable text (image-only)
      'none'    — no PDF at all
    Returns rows processed.
    """
    rows = conn.execute(
        "SELECT case_id, pdf_path FROM aaibmn_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    for row in rows:
        pdf_path = row["pdf_path"]
        full_text = extract_text(pdf_path) if pdf_path else ""

        if len(full_text) >= MIN_NARRATIVE:
            narrative, tier = full_text, "pdf"
        elif full_text:
            narrative, tier = full_text, "short"
        elif pdf_path:
            narrative, tier = "", "scanned"
        else:
            narrative, tier = "", "none"

        conn.execute(
            "UPDATE aaibmn_reports "
            "SET narrative_text=?, source_tier=?, status=?, updated_at=? "
            "WHERE case_id=?",
            (narrative, tier, db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()

    return len(rows)


def build(conn):
    """Project parsed rows into aaibmn_accidents.

    Narrative fallback: when the extracted narrative is below the floor (scanned
    PDFs yield none), fall back to the listing title so the occurrence is still
    built. Rows with neither narrative nor title are skipped.
    Returns rows built.
    """
    rows = conn.execute(
        "SELECT case_id, title, event_class, aircraft, registration, operator, "
        "location, date_of_occurrence, narrative_text, source_tier, pdf_url, report_url "
        "FROM aaibmn_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = (row["narrative_text"] or "").strip()
        title = (row["title"] or "").strip()
        if len(narrative) < _NARRATIVE_FLOOR:
            # scanned / empty extraction → use the listing title as narrative
            narrative = title or narrative

        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE aaibmn_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        source_url = row["pdf_url"] or row["report_url"]
        site_slug = text.make_site_slug(row["aircraft"], row["registration"], title)

        conn.execute(
            "INSERT OR REPLACE INTO aaibmn_accidents "
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
                "MN",
                narrative,
                None,
                source_url,
                row["event_class"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE aaibmn_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    return built
