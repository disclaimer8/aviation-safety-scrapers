# griaa_ingest/pipeline.py
"""
discover → fetch → parse → build pipeline for GRIAA (Colombia) per-year listing.

discover(): walks every per-year listing URL (GET ?inicio=Y&fin=Y, which bypasses
the filter widget's captcha) and INSERTs new case_ids into griaa_reports with
listing metadata.  Idempotent — existing case_ids are skipped.

fetch(): for each status='new' row with a pdf_url, downloads the PDF and advances
to 'fetched'.  Per-row try/except: a download failure keeps the row at 'new' for
the next run.

parse(): extracts text via pdftotext and strips the ADVERTENCIA legal preamble.
  source_tier:
    'pdf'     — usable text layer (>= SCANNED_THRESHOLD chars after stripping)
    'scanned' — text empty / trivially short (< SCANNED_THRESHOLD): image-only PDF
    'none'    — no PDF / extraction failed

build(): emits griaa_accidents rows.  Rows whose source_tier is 'scanned'/'none',
or whose narrative is shorter than _NARRATIVE_FLOOR, are skipped — scanned PDFs
have no usable narrative for the downstream LLM rewrite.
"""
import os
import sys
import time

from . import griaa, db, text
from .pdf import extract_text, SCANNED_THRESHOLD

_NARRATIVE_FLOOR = 80  # chars; rows with less are treated as non-report events


def discover(conn, client, full=False):
    """Walk per-year listings and INSERT new case_ids into griaa_reports.

    full: accepted for API parity (the whole year range is always walked;
          per-case_id skip handles idempotency).

    Returns: number of rows inserted.
    """
    inserted = 0
    for url in griaa.iter_year_urls():
        time.sleep(griaa.DELAY)
        try:
            resp = client.get(url)
            resp.raise_for_status()
            html = resp.content.decode("utf-8", "replace") if isinstance(resp.content, bytes) else resp.content
        except Exception as exc:
            print(f"[griaa discover] {url}: {exc}", file=sys.stderr)
            continue

        rows = griaa.parse_listing(html, url)
        for row in rows:
            case_id = row["case_id"]
            if conn.execute(
                "SELECT 1 FROM griaa_reports WHERE case_id=?", (case_id,)
            ).fetchone():
                continue  # already known

            pdf_url = row.get("pdf_url_es")
            lang = "es" if pdf_url else None

            ts = db.now_ms()
            conn.execute(
                "INSERT INTO griaa_reports "
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
    """Download PDFs for status='new' rows and advance them to 'fetched'.

    Rows with no pdf_url advance to 'fetched' with pdf_path=None.
    Per-row try/except: a download failure keeps the row at 'new' for retry.

    Returns: number of rows iterated (including failures).
    """
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, pdf_url FROM griaa_reports WHERE status=?",
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
                time.sleep(griaa.DELAY)
                griaa.download(client, pdf_url, dest)
                pdf_path = dest
            except Exception as exc:
                print(f"[griaa fetch] {case_id}: download {exc}", file=sys.stderr)
                continue  # stay at 'new' for retry

        try:
            conn.execute(
                "UPDATE griaa_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[griaa fetch] {case_id}: db {exc}", file=sys.stderr)

    return len(rows)


def parse(conn):
    """Extract + clean PDF text for status='fetched' rows.

    Strips the ADVERTENCIA legal preamble.  source_tier:
      'pdf'     — cleaned text length >= SCANNED_THRESHOLD
      'scanned' — text present but below threshold (image-only scan)
      'none'    — no PDF / empty extraction

    Returns: number of rows processed.
    """
    rows = conn.execute(
        "SELECT case_id, pdf_path FROM griaa_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    for row in rows:
        pdf_path = row["pdf_path"]
        raw = extract_text(pdf_path) if pdf_path else ""
        cleaned = text.strip_advertencia(raw) if raw else ""

        if len(cleaned) >= SCANNED_THRESHOLD:
            narrative = cleaned
            tier = "pdf"
        elif cleaned:
            narrative = cleaned
            tier = "scanned"
        else:
            narrative = ""
            tier = "none"

        conn.execute(
            "UPDATE griaa_reports "
            "SET narrative_text=?, source_tier=?, status=?, updated_at=? "
            "WHERE case_id=?",
            (narrative, tier, db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()

    return len(rows)


def build(conn):
    """Emit a griaa_accidents record per buildable status='parsed' row.

    Skip criteria (status → 'skipped'):
      • source_tier is 'scanned' or 'none' (no usable text layer), OR
      • narrative_text shorter than _NARRATIVE_FLOOR chars.

    Returns: number of rows built (not skipped).
    """
    rows = conn.execute(
        "SELECT case_id, event_class, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, source_tier, pdf_url, report_url "
        "FROM griaa_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        tier = row["source_tier"]
        if tier in ("scanned", "none") or len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE griaa_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        source_url = row["pdf_url"] or row["report_url"]
        site_slug = text.make_site_slug(row["case_id"])

        conn.execute(
            "INSERT OR REPLACE INTO griaa_accidents "
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
                "CO",
                narrative,
                None,
                source_url,
                row["event_class"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE griaa_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    return built
