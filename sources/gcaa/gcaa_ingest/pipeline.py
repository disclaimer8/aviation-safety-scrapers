# gcaa_ingest/pipeline.py
"""
discover → fetch(+parse) → build pipeline for GCAA UAE.

discover() issues ONE SharePoint REST GET (all 171 items, $expand=
AttachmentFiles).  For each item it derives case_id from Reference_No
(fallback gcaa-{Id}); items with NO attachment are stubs and skipped
(mirrors the jst doc-less skip).  Full metadata — registration, aircraft,
location, occurrence date, category, report status, year — comes straight
from the JSON.  The preferred attachment (a 'Final' report when present)
supplies the PDF.

fetch() downloads the chosen PDF, pdftotext, tiers pdf/scanned, advances to
'parsed'.  A download/extract failure leaves the row 'new' for retry.

build() promotes 'parsed' rows with narrative >= floor into gcaa_accidents
(country AE, source_url = the PDF URL, report_type = report_status).
"""
import os
import sys
import time

from . import db, gcaa, pdf
from .text import make_site_slug

_NARRATIVE_FLOOR = 300


def discover(conn, client, **_kw):
    """
    One API GET; INSERT new attachment-bearing rows keyed on case_id.
    Stub rows (no AttachmentFiles) are skipped.
    """
    items = gcaa.fetch_items(client)
    existing = {
        r["case_id"] for r in conn.execute("SELECT case_id FROM gcaa_reports")
    }
    inserted = 0
    for item in items:
        meta = gcaa.parse_item(item)
        case_id = meta["case_id"]
        if not case_id:
            continue
        if not meta["has_attachment"] or not meta["pdf_url"]:
            continue  # stub — no report PDF, skip like jst doc-less events
        if case_id in existing:
            continue
        existing.add(case_id)
        ts = db.now_ms()
        conn.execute(
            "INSERT INTO gcaa_reports "
            "(case_id, reference_no, item_id, filename, server_relative_url, "
            "aircraft, registration, occurrence_category, report_status, "
            "date_of_occurrence, location, damage, year, pdf_url, status, "
            "discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                case_id,
                meta["reference_no"],
                meta["item_id"],
                meta["filename"],
                meta["server_relative_url"],
                meta["aircraft"],
                meta["registration"],
                meta["occurrence_category"],
                meta["report_status"],
                meta["date"],
                meta["location"],
                meta["damage"],
                meta["year"],
                meta["pdf_url"],
                db.STATUS_NEW,
                ts,
                ts,
            ),
        )
        inserted += 1
    conn.commit()
    return inserted


def fetch(conn, client, pdf_dir="pdfs"):
    """
    For each status='new' row: download the chosen PDF, pdftotext, tier,
    advance to 'parsed'.  Failing rows stay 'new'.
    """
    rows = conn.execute(
        "SELECT case_id, pdf_url FROM gcaa_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()
    os.makedirs(pdf_dir, exist_ok=True)
    for row in rows:
        case_id = row["case_id"]
        pdf_path = os.path.join(pdf_dir, f"{case_id}.pdf")
        url = row["pdf_url"]
        if not url:
            continue
        time.sleep(gcaa.DELAY)
        try:
            gcaa.download_pdf(client, url, pdf_path)
            text = pdf.extract_text(pdf_path)
        except Exception as e:
            print(f"[gcaa fetch] {case_id}: {url}: {e}", file=sys.stderr)
            continue  # stays 'new' for retry next cycle

        tier = "pdf" if len(text or "") >= _NARRATIVE_FLOOR else "scanned"
        try:
            conn.execute(
                "UPDATE gcaa_reports SET narrative_text=?, source_tier=?, "
                "pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (text, tier, pdf_path, db.STATUS_PARSED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as e:
            print(f"[gcaa fetch] {case_id}: db update failed: {e}",
                  file=sys.stderr)
    return len(rows)


def build(conn):
    """Promote 'parsed' rows with narrative >= floor into gcaa_accidents."""
    rows = conn.execute(
        "SELECT case_id, report_status, aircraft, registration, location, "
        "date_of_occurrence, narrative_text, pdf_url "
        "FROM gcaa_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()
    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE gcaa_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        site_slug = make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )
        conn.execute(
            "INSERT OR REPLACE INTO gcaa_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, "
            "country, narrative_text, probable_cause, source_url, report_type, "
            "site_slug, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["case_id"],
                row["date_of_occurrence"],
                row["aircraft"],
                row["registration"],
                None,
                row["location"],
                "AE",
                narrative,
                None,
                row["pdf_url"] or "https://www.gcaa.gov.ae",
                row["report_status"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE gcaa_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1
    return built
