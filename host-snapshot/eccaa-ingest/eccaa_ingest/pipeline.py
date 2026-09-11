# eccaa_ingest/pipeline.py
"""
discover -> fetch -> parse -> build pipeline for ECCAA (Eastern Caribbean CAA).

discover(): GET the single AIG Reports article, parse the final-report PDF links,
  INSERT new case_ids into eccaa_reports with metadata (incl. per-report country
  derived from the registration prefix). Idempotent — existing case_ids skipped.

fetch(): for each status='new' row with a pdf_url: download the (percent-encoded)
  PDF and advance to 'fetched'. Per-row try/except; a download failure keeps the
  row at 'new' for the next run.

parse(): pdftotext extraction. tier 'pdf' when len>=MIN_NARRATIVE (600), 'short'
  when some text but below, 'none' when empty. Scanned reports (no text layer)
  land at 'short'/'none' and are skipped by build().

build(): emits eccaa_accidents rows; rows below _NARRATIVE_FLOOR (80 chars,
  i.e. effectively scanned/no-text reports) -> 'skipped'. country comes from the
  per-report value captured at discover time.
"""
import os
import sys
import time

from . import eccaa, db, text
from .pdf import extract_text, MIN_NARRATIVE

_NARRATIVE_FLOOR = 80  # chars; below this a report is treated as scanned/empty


def discover(conn, client, full=False):
    """GET the AIG Reports article and INSERT new case_ids into eccaa_reports.

    full: accepted for API parity (the single listing is always fully walked).

    Returns: number of rows inserted.
    """
    resp = client.get(eccaa.INDEX_URL)
    resp.raise_for_status()
    html = resp.content.decode("utf-8", "replace") if isinstance(resp.content, bytes) else resp.content

    rows = eccaa.parse_listing(html)

    inserted = 0
    for row in rows:
        case_id = row["case_id"]
        if conn.execute(
            "SELECT 1 FROM eccaa_reports WHERE case_id=?", (case_id,)
        ).fetchone():
            continue  # already known

        ts = db.now_ms()
        conn.execute(
            "INSERT INTO eccaa_reports "
            "(case_id, report_url, pdf_url, pdf_url_es, pdf_url_en, "
            "title, event_class, aircraft, registration, date_of_occurrence, "
            "location, country, lang, status, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                row.get("country"),
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
    """Download PDFs for status='new' rows; advance to 'fetched'.

    Per-row try/except: a download failure keeps the row at 'new' for retry.

    Returns: number of rows iterated.
    """
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, pdf_url FROM eccaa_reports WHERE status=?",
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
                time.sleep(eccaa.DELAY)
                eccaa.download(client, pdf_url, dest)
                pdf_path = dest
            except Exception as exc:
                print(f"[eccaa fetch] {case_id}: download {exc}", file=sys.stderr)
                continue  # stay at 'new' for retry

        try:
            conn.execute(
                "UPDATE eccaa_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[eccaa fetch] {case_id}: db {exc}", file=sys.stderr)

    return len(rows)


def parse(conn):
    """Extract PDF text for status='fetched' rows. Returns rows processed."""
    rows = conn.execute(
        "SELECT case_id, pdf_path FROM eccaa_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    for row in rows:
        pdf_path = row["pdf_path"]
        full_text = extract_text(pdf_path) if pdf_path else ""

        if len(full_text) >= MIN_NARRATIVE:
            narrative, tier = full_text, "pdf"
        elif full_text:
            narrative, tier = full_text, "short"
        else:
            narrative, tier = "", "none"

        conn.execute(
            "UPDATE eccaa_reports "
            "SET narrative_text=?, source_tier=?, status=?, updated_at=? "
            "WHERE case_id=?",
            (narrative, tier, db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()

    return len(rows)


def build(conn):
    """Emit eccaa_accidents rows; skip scanned/empty (< _NARRATIVE_FLOOR).

    country comes from the per-report value (NOT a single fixed constant).
    source_url: pdf_url if present, else report_url.

    Returns: number of rows built.
    """
    rows = conn.execute(
        "SELECT case_id, event_class, aircraft, registration, operator, location, "
        "country, date_of_occurrence, narrative_text, pdf_url, report_url "
        "FROM eccaa_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE eccaa_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        source_url = row["pdf_url"] or row["report_url"]
        site_slug = text.make_site_slug(row["aircraft"], row["registration"], row["location"])
        country = row["country"] or text.DEFAULT_COUNTRY

        conn.execute(
            "INSERT OR REPLACE INTO eccaa_accidents "
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
                country,
                narrative,
                None,
                source_url,
                row["event_class"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE eccaa_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    return built
